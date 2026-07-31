from __future__ import annotations

from pathlib import Path

from app.models import (
    AnalysisMode,
    JobEvent,
    JobStatus,
    Track,
    VideoMetadata,
)
from app.store import Store


def _track(position: int, seconds: int, title: str) -> Track:
    return Track(
        position=position,
        timestamp_seconds=seconds,
        timestamp=f"{seconds // 60:02d}:{seconds % 60:02d}",
        artist="Artist",
        title=title,
        source="shazam",
    )


def test_create_and_get_job(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite", tmp_path / "audio")
    job = store.create_job(
        job_id="abc",
        url="https://example.com",
        canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        video_id="dQw4w9WgXcQ",
        mode=AnalysisMode.thorough,
    )
    assert job.status == JobStatus.queued
    fetched = store.get_job("abc")
    assert fetched.id == "abc"


def test_complete_and_replace_tracks(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite", tmp_path / "audio")
    store.create_job(
        job_id="job1",
        url="u",
        canonical_url="c",
        video_id="v",
        mode=AnalysisMode.thorough,
    )
    metadata = VideoMetadata(
        title="Demo", duration_seconds=120, duration="02:00", youtube_url="c"
    )
    store.complete_job(
        "job1",
        metadata,
        [_track(1, 0, "A"), _track(2, 30, "B"), _track(3, 60, "C")],
    )
    job = store.get_job("job1")
    assert job.status == JobStatus.completed
    assert job.tracks[0].title == "A"
    store.replace_tracks_in_range("job1", 20, 50, [_track(1, 20, "B2")])
    job = store.get_job("job1")
    assert [track.title for track in job.tracks] == ["A", "B2", "C"]
    assert [track.position for track in job.tracks] == [1, 2, 3]


def test_audio_and_fingerprint_cache(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite", tmp_path / "audio")
    audio_source = tmp_path / "in.m4a"
    audio_source.write_bytes(b"faux audio")
    stored = store.store_audio("video1", audio_source, duration_seconds=180)
    assert stored.is_file()
    assert store.get_audio_path("video1") == stored
    fingerprint_path = tmp_path / "fp.bin"
    fingerprint_path.write_bytes(b"sample")
    sample_hash = Store.hash_sample(fingerprint_path)
    assert store.get_fingerprint(sample_hash) is None
    payload = {"track": {"title": "X"}, "matches": []}
    store.store_fingerprint(sample_hash, payload)
    cached = store.get_fingerprint(sample_hash)
    assert cached == payload


def test_rescan_section_lifecycle(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite", tmp_path / "audio")
    store.create_job(
        job_id="job2",
        url="u",
        canonical_url="c",
        video_id="v",
        mode=AnalysisMode.thorough,
    )
    store.set_rescan_section("job2", 30, 90)
    assert store.pop_rescan_section("job2") == (30, 90)
    assert store.pop_rescan_section("job2") is None


def test_tail_events_records_state_changes(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite", tmp_path / "audio")
    store.create_job(
        job_id="job3",
        url="u",
        canonical_url="c",
        video_id="v",
        mode=AnalysisMode.thorough,
    )
    store.update_job_state(
        "job3", status=JobStatus.inspecting, progress=10, message="Reading"
    )
    store.complete_job(
        "job3",
        VideoMetadata(
            title="x", duration_seconds=10, duration="00:10", youtube_url="c"
        ),
        [],
    )
    events = store.tail_events("job3")
    assert [event.status for event in events] == [
        JobStatus.queued,
        JobStatus.inspecting,
        JobStatus.completed,
    ]


def test_subscribers_receive_broadcast(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite", tmp_path / "audio")
    store.create_job(
        job_id="job4",
        url="u",
        canonical_url="c",
        video_id="v",
        mode=AnalysisMode.thorough,
    )
    subscriber = store.subscribe("job4")
    event = JobEvent(status=JobStatus.downloading, progress=30, message="Working")
    store.broadcast("job4", event)
    assert subscriber.get_nowait() == event
    store.unsubscribe("job4", subscriber)
    store.unsubscribe_all("job4")
