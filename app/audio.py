from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg

logger = logging.getLogger("trackid.audio")

SILENCE_PATTERN = re.compile(
    r"silence_(?P<event>start|end): (?P<value>-?\d+(?:\.\d+)?)"
)


@dataclass(frozen=True)
class Transition:
    position_seconds: float
    duration_seconds: float


def ffmpeg_executable() -> str:
    return str(imageio_ffmpeg.get_ffmpeg_exe())


def create_sample(
    audio_path: Path,
    sample_path: Path,
    position: int,
    *,
    duration_seconds: int = 12,
    ffmpeg: str | None = None,
    timeout: float = 45.0,
) -> None:
    command = [
        ffmpeg or ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(position),
        "-i",
        str(audio_path),
        "-t",
        str(duration_seconds),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "96k",
        "-y",
        str(sample_path),
    ]
    subprocess.run(
        command, check=True, capture_output=True, timeout=timeout
    )


def detect_transitions(
    audio_path: Path,
    *,
    noise_floor_db: float = -32.0,
    min_silence_seconds: float = 0.4,
    ffmpeg: str | None = None,
    timeout: float = 120.0,
) -> list[Transition]:
    command = [
        ffmpeg or ffmpeg_executable(),
        "-hide_banner",
        "-i",
        str(audio_path),
        "-af",
        f"silencedetect=noise={noise_floor_db}dB:d={min_silence_seconds}",
        "-f",
        "null",
        "-",
    ]
    process = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    transitions: list[Transition] = []
    pending_start: float | None = None
    for line in (process.stderr or "").splitlines():
        match = SILENCE_PATTERN.search(line)
        if not match:
            continue
        value = float(match.group("value"))
        if match.group("event") == "start":
            pending_start = value
        elif pending_start is not None and value > pending_start:
            transitions.append(
                Transition(
                    position_seconds=value,
                    duration_seconds=value - pending_start,
                )
            )
            pending_start = None
    return _merge_transitions(transitions)


def iter_chunks(
    audio_path: Path,
    chunk_seconds: int,
) -> Iterator[tuple[int, bytes]]:
    ffmpeg = ffmpeg_executable()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        "8000",
        "-",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert process.stdout is not None
    bytes_per_chunk = chunk_seconds * 8000 * 2
    position = 0
    try:
        while True:
            data = process.stdout.read(bytes_per_chunk)
            if not data:
                break
            yield position, data
            position += len(data) // (8000 * 2)
    finally:
        if process.poll() is None:
            process.terminate()


def audio_metadata(audio_path: Path) -> dict[str, float]:
    command = [
        ffmpeg_executable(),
        "-hide_banner",
        "-i",
        str(audio_path),
        "-f",
        "ffmetadata",
        "-",
    ]
    process = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=60
    )
    info: dict[str, float] = {}
    for line in (process.stderr or "").splitlines():
        if line.startswith("  Duration:"):
            parts = line.split("Duration:", 1)[1].split(",")[0].strip()
            if ":" in parts:
                hours, minutes, seconds = parts.split(":")
                info["duration"] = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return info


def _merge_transitions(transitions: list[Transition]) -> list[Transition]:
    merged: list[Transition] = []
    minimum_gap = 4.0
    for transition in sorted(transitions, key=lambda item: item.position_seconds):
        if merged and transition.position_seconds - merged[-1].position_seconds < minimum_gap:
            continue
        merged.append(transition)
    return merged


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
