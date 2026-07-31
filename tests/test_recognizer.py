from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from app.recognizer import (
    RecognitionResult,
    TracklistAnalyzer,
    build_tracklist,
    parse_recognition,
    split_mix,
)
from app.store import Store


def recognition(
    position: int, fingerprint: str, offset: float | None = None, mix: str | None = None
) -> RecognitionResult:
    return RecognitionResult(
        sample_position=position,
        fingerprint=fingerprint,
        artist=f"Artist {fingerprint}",
        title=f"Track {fingerprint}",
        mix=mix,
        offset=offset,
        cover_url=None,
        track_url=None,
    )


def test_collapses_consecutive_recognitions() -> None:
    samples = [
        recognition(15, "a", 15),
        recognition(60, "a", 60),
        recognition(105, "b", 15),
        recognition(150, "b", 60),
        recognition(195, "c"),
    ]

    tracks = build_tracklist(samples, sample_interval=45)

    assert [(track.title, track.timestamp_seconds) for track in tracks] == [
        ("Track a", 0),
        ("Track b", 90),
        ("Track c", 172),
    ]
    assert tracks[0].source == "shazam"


def test_returns_empty_tracklist_without_matches() -> None:
    assert build_tracklist([], sample_interval=45) == []


@pytest.mark.parametrize(
    ("raw", "title", "mix"),
    [
        ("Sunset Vibes (Original Mix)", "Sunset Vibes", "Original Mix"),
        ("Dreamer [Extended Mix]", "Dreamer", "Extended Mix"),
        ("Silhouette", "Silhouette", None),
        ("Reverie (Club Mix) [Radio Edit]", "Reverie (Club Mix)", "Radio Edit"),
    ],
)
def test_split_mix_extracts_version(raw: str, title: str, mix: str | None) -> None:
    result_title, result_mix = split_mix(raw)
    assert result_title == title
    assert result_mix == mix


def test_parse_recognition_extracts_offset_and_cover() -> None:
    result = {
        "track": {
            "title": "Echoes",
            "subtitle": "Maya Cole",
            "url": "https://example.com/track",
            "key": "abc",
            "images": {"coverart": "https://example.com/cover.jpg"},
        },
        "matches": [{"offset": "13.4"}],
    }
    recognition_result = parse_recognition(result, sample_position=180)
    assert recognition_result is not None
    assert recognition_result.artist == "Maya Cole"
    assert recognition_result.title == "Echoes"
    assert recognition_result.offset == 13.4
    assert recognition_result.cover_url == "https://example.com/cover.jpg"


def test_parse_recognition_without_track_returns_none() -> None:
    assert parse_recognition({"track": None}, 10) is None
    assert parse_recognition({}, 10) is None


class _BlockingShazam:
    async def recognize(self, _path: str) -> dict[str, Any]:
        await asyncio.sleep(60)
        return {}


class _SlowShazam:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    async def recognize(self, _path: str) -> dict[str, Any]:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return {"track": None}


@pytest.mark.asyncio
async def test_analyzer_skips_samples_when_shazam_hangs(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite", tmp_path / "audio")
    audio_path = tmp_path / "fake.m4a"
    audio_path.write_bytes(b"\x00")
    analyzer = TracklistAnalyzer(store)
    analyzer._shazam_timeout = 0.5

    semaphore = asyncio.Semaphore(1)

    def fake_cutting(_audio_path: Path, sample_path: Path, _position: int) -> None:
        sample_path.write_bytes(b"\x00")

    import app.recognizer as recognizer_module

    original = recognizer_module.create_sample
    recognizer_module.create_sample = fake_cutting
    try:
        positions = list(range(0, 60, 6))
        results = await asyncio.gather(
            *(
                analyzer._recognize_one(
                    position=position,
                    audio_path=audio_path,
                    job_id="test",
                    semaphore=semaphore,
                    shazam=_BlockingShazam(),
                )
                for position in positions
            ),
            return_exceptions=True,
        )
    finally:
        recognizer_module.create_sample = original
    completed = [r for r in results if r is None or isinstance(r, RecognitionResult)]
    assert len(completed) == len(positions)
    assert all(result is None for result in completed)


@pytest.mark.asyncio
async def test_progress_completes_after_timeouts(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite", tmp_path / "audio")
    audio_path = tmp_path / "fake.m4a"
    audio_path.write_bytes(b"\x00")
    analyzer = TracklistAnalyzer(store)
    analyzer._shazam_timeout = 0.2

    def fast_cut(_audio_path: Path, sample_path: Path, _position: int) -> None:
        sample_path.write_bytes(b"\x00")

    import app.recognizer as recognizer_module

    original = recognizer_module.create_sample
    recognizer_module.create_sample = fast_cut
    try:
        started = time.monotonic()
        semaphore = asyncio.Semaphore(1)
        results = await asyncio.gather(
            *(
                analyzer._recognize_one(
                    position=position,
                    audio_path=audio_path,
                    job_id="test",
                    semaphore=semaphore,
                    shazam=_SlowShazam(0.5),
                )
                for position in range(0, 30, 6)
            ),
            return_exceptions=True,
        )
        elapsed = time.monotonic() - started
    finally:
        recognizer_module.create_sample = original
    assert elapsed < 5
    assert all(r is None for r in results)
