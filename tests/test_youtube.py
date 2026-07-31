import pytest

from app.youtube import (
    InvalidYouTubeUrl,
    extract_video_id,
    format_timestamp,
    parse_chapter_tracklist,
    parse_description_tracklist,
    split_artist_title,
    timestamp_to_seconds,
)


@pytest.mark.parametrize(
    ("url", "video_id"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=10", "dQw4w9WgXcQ"),
        ("https://youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ],
)
def test_extracts_youtube_video_id(url: str, video_id: str) -> None:
    assert extract_video_id(url) == video_id


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com.evil.test/watch?v=dQw4w9WgXcQ",
        "file:///tmp/audio.mp3",
        "https://youtube.com/watch?v=short",
        "not a url",
    ],
)
def test_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(InvalidYouTubeUrl):
        extract_video_id(url)


def test_timestamp_conversion() -> None:
    assert timestamp_to_seconds("03:42") == 222
    assert timestamp_to_seconds("1:03:42") == 3822
    assert format_timestamp(222) == "03:42"
    assert format_timestamp(3822) == "1:03:42"


def test_parses_common_description_formats() -> None:
    description = """
00:00 Artist One - Opening Track
2. [04:12] Artist Two — Second Track
Artist Three - Closing Track (1:02:09)
https://example.com 03:00
"""

    tracks = parse_description_tracklist(description, duration=4000)

    assert [(track.timestamp_seconds, track.artist, track.title) for track in tracks] == [
        (0, "Artist One", "Opening Track"),
        (252, "Artist Two", "Second Track"),
        (3729, "Artist Three", "Closing Track"),
    ]


def test_parses_chapters_and_splits_labels() -> None:
    tracks = parse_chapter_tracklist(
        [
            {"start_time": 0, "title": "Intro"},
            {"start_time": 95.4, "title": "Artist - Song"},
        ]
    )

    assert tracks[0].artist is None
    assert tracks[0].title == "Intro"
    assert tracks[1].artist == "Artist"
    assert tracks[1].title == "Song"
    assert split_artist_title("No separator") == (None, "No separator")
