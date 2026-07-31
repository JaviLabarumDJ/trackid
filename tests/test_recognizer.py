from __future__ import annotations

import pytest

from app.recognizer import (
    RecognitionResult,
    build_tracklist,
    parse_recognition,
    split_mix,
)


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
