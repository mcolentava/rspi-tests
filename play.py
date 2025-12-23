#!/usr/bin/env python3
"""
PCM5100 (I2S) playback smoke test for Raspberry Pi 5.

Plays an MP3 file via ALSA using whichever backend is available:
  1) mpg123 (preferred)
  2) ffplay
  3) ffmpeg -> wav -> aplay

Wiring reminder (PCM5100 to Pi):
  VIN/5V  -> Pin 02 (5V)
  GND     -> Pin 06 (GND)
  BCK     -> Pin 12 (GPIO18 / I2S BCLK)
  LRCK/WS -> Pin 35 (GPIO19 / I2S LRCLK)
  DIN     -> Pin 40 (GPIO21 / I2S DOUT)
  SCK     -> GND
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class Cmd:
    exe: str
    args: list[str]

    def as_list(self) -> list[str]:
        return [self.exe, *self.args]

    def pretty(self) -> str:
        return " ".join(shlex.quote(x) for x in self.as_list())


def _run_capture(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _read_text_if_exists(p: Path) -> Optional[str]:
    try:
        return p.read_text(errors="replace").strip()
    except FileNotFoundError:
        return None


def _is_raspberry_pi() -> bool:
    # Best-effort checks (works on Pi OS / Debian variants)
    model = _read_text_if_exists(Path("/proc/device-tree/model"))
    if model and "Raspberry Pi" in model:
        return True
    if platform.machine().startswith(("aarch64", "arm")) and Path("/sys/firmware/devicetree/base/model").exists():
        m2 = _read_text_if_exists(Path("/sys/firmware/devicetree/base/model"))
        return bool(m2 and "Raspberry Pi" in m2)
    return False


def _aplay_list_cards() -> str:
    aplay = _which("aplay")
    if not aplay:
        return ""
    return _run_capture([aplay, "-l"]).stdout


def _aplay_list_pcms() -> str:
    aplay = _which("aplay")
    if not aplay:
        return ""
    return _run_capture([aplay, "-L"]).stdout


def _guess_pcm5100_card_present(aplay_l: str, aplay_L: str) -> bool:
    # Common overlays/names for I2S DACs (PCM5102/PCM5122/PCM5100 class)
    hay = (aplay_l + "\n" + aplay_L).lower()
    keywords = [
        "hifiberry",
        "i2s",
        "snd_rpi_hifiberry_dac",
        "sndrpihifiberry",
        "rpi-dac",
        "rpidac",
        "dac",
        "pcm51",
    ]
    return any(k in hay for k in keywords)


def _print_i2s_setup_hint() -> None:
    msg = """
PCM5100/I2S card not detected in ALSA output.

Quick checklist (Pi OS Bookworm / Raspberry Pi 5):
  - Enable I2S and an I2S DAC overlay in `/boot/firmware/config.txt`, then reboot.
    Common working overlay for PCM5100 boards:
      dtparam=i2s=on
      dtoverlay=hifiberry-dac

After reboot, verify:
  - `aplay -l` shows an extra sound card for the I2S DAC
  - Then run this script again (optionally with `--device plughw:<card>,<dev>`).
""".strip()
    print(msg, file=sys.stderr)


def _backend_commands(mp3_path: Path, device: Optional[str], loops: int) -> Iterable[Cmd]:
    """
    Yield candidate playback commands in preference order.
    """
    mpg123 = _which("mpg123")
    if mpg123:
        args = []
        if device:
            # mpg123: -a dev (e.g. "hw:1,0" or "plughw:1,0")
            args += ["-a", device]
        if loops < 0:
            args += ["--loop", "-1"]
        elif loops > 1:
            args += ["--loop", str(loops)]
        args += [str(mp3_path)]
        yield Cmd(mpg123, args)

    ffplay = _which("ffplay")
    if ffplay:
        # ffplay default output device is system default; piping to ALSA device is not portable.
        # Still useful if user has set default card to the I2S DAC.
        args = ["-nodisp", "-autoexit", "-loglevel", "error"]
        if loops < 0:
            args += ["-loop", "0"]
        elif loops > 1:
            args += ["-loop", str(loops - 1)]
        args += [str(mp3_path)]
        yield Cmd(ffplay, args)

    ffmpeg = _which("ffmpeg")
    aplay = _which("aplay")
    if ffmpeg and aplay:
        # Decode to a temp WAV then play it; lets us pass an ALSA device to aplay.
        # (PCM5100 is 16/24-bit capable; we'll keep it simple: 44100Hz stereo 16-bit)
        # Note: ffmpeg supports "-f wav -" piping, but aplay device selection + stdin is flaky across distros.
        yield Cmd("__ffmpeg_aplay__", [str(mp3_path), device or "default", str(loops)])


def _run_ffmpeg_to_wav_then_aplay(mp3_path: Path, device: str, loops: int) -> int:
    ffmpeg = _which("ffmpeg")
    aplay = _which("aplay")
    if not ffmpeg or not aplay:
        return 127

    with tempfile.TemporaryDirectory(prefix="pcm5100_test_") as td:
        wav_path = Path(td) / "decoded.wav"
        decode = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(mp3_path),
            "-ac",
            "2",
            "-ar",
            "44100",
            str(wav_path),
        ]
        p1 = subprocess.run(decode, check=False)
        if p1.returncode != 0:
            return p1.returncode

        play = [aplay]
        if device and device != "default":
            play += ["-D", device]

        # loops: -1 = infinite, N>=1 = play N times
        if loops < 0:
            while True:
                p2 = subprocess.run(play + [str(wav_path)], check=False)
                if p2.returncode != 0:
                    return p2.returncode
        else:
            for _ in range(max(1, loops)):
                p2 = subprocess.run(play + [str(wav_path)], check=False)
                if p2.returncode != 0:
                    return p2.returncode
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Play an MP3 through PCM5100 (I2S) via ALSA on Raspberry Pi.")
    parser.add_argument(
        "--file",
        default="track1.mp3",
        help="Path to MP3 to play (default: track1.mp3 in current directory).",
    )
    parser.add_argument(
        "--device",
        default=None,
        help=(
            "ALSA device override (examples: 'default', 'hw:1,0', 'plughw:1,0'). "
            "If omitted, uses system default unless backend supports selection."
        ),
    )
    parser.add_argument(
        "--loops",
        type=int,
        default=1,
        help="Number of times to play (default: 1). Use -1 for infinite loop.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print `aplay -l` and `aplay -L` output then exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command that would be run, but do not play audio.",
    )

    args = parser.parse_args()

    mp3_path = Path(args.file).expanduser().resolve()
    if not mp3_path.exists():
        print(f"File not found: {mp3_path}", file=sys.stderr)
        return 2

    aplay_l = _aplay_list_cards()
    aplay_L = _aplay_list_pcms()

    if args.list_devices:
        print("=== aplay -l ===")
        print(aplay_l.strip() if aplay_l.strip() else "(aplay not found or no output)")
        print("\n=== aplay -L ===")
        print(aplay_L.strip() if aplay_L.strip() else "(aplay not found or no output)")
        return 0

    if _is_raspberry_pi():
        if not _guess_pcm5100_card_present(aplay_l, aplay_L):
            _print_i2s_setup_hint()

    candidates = list(_backend_commands(mp3_path, args.device, args.loops))
    if not candidates:
        print(
            "No playback backend found. Install one of: mpg123, ffplay (ffmpeg), or ffmpeg+aplay.\n"
            "Examples:\n"
            "  sudo apt update\n"
            "  sudo apt install -y mpg123\n"
            "or:\n"
            "  sudo apt install -y ffmpeg alsa-utils",
            file=sys.stderr,
        )
        return 127

    for cmd in candidates:
        if cmd.exe == "__ffmpeg_aplay__":
            mp3_arg, device_arg, loops_arg = cmd.args
            msg = f"Using backend: ffmpeg + aplay (device={device_arg})"
            print(msg)
            if args.dry_run:
                print("Would run: ffmpeg <mp3> -> wav -> aplay")
                return 0
            return _run_ffmpeg_to_wav_then_aplay(Path(mp3_arg), device_arg, int(loops_arg))

        print(f"Using backend: {Path(cmd.exe).name}")
        print(f"Running: {cmd.pretty()}")
        if args.dry_run:
            return 0

        try:
            p = subprocess.run(cmd.as_list(), check=False)
        except FileNotFoundError:
            continue
        if p.returncode == 0:
            return 0

        print(f"Backend failed with exit code {p.returncode}; trying next option...", file=sys.stderr)

    print("All playback backends failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


