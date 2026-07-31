from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    AnalysisMode,
    Job,
    JobEvent,
    JobStatus,
    Track,
    VideoMetadata,
)

logger = logging.getLogger("trackid.store")


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    video_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    metadata_json TEXT,
    tracks_json TEXT,
    error TEXT,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL,
    message TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS job_events_job_idx ON job_events(job_id, id);

CREATE TABLE IF NOT EXISTS audio_cache (
    video_id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    created_at REAL NOT NULL,
    last_accessed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fingerprint_cache (
    sample_hash TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_accessed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS section_recognitions (
    job_id TEXT NOT NULL,
    sample_position INTEGER NOT NULL,
    sample_hash TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    artist TEXT,
    title TEXT,
    mix TEXT,
    offset_seconds REAL,
    cover_url TEXT,
    track_url TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (job_id, sample_position)
);

CREATE INDEX IF NOT EXISTS section_recognitions_job_idx
    ON section_recognitions(job_id);

CREATE TABLE IF NOT EXISTS rescan_requests (
    job_id TEXT PRIMARY KEY,
    start_seconds INTEGER NOT NULL,
    end_seconds INTEGER NOT NULL,
    requested_at REAL NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
"""


@dataclass(frozen=True)
class SectionRecognition:
    sample_position: int
    fingerprint: str
    artist: str | None
    title: str
    mix: str | None
    offset: float | None
    cover_url: str | None
    track_url: str | None


class Store:
    def __init__(self, db_path: Path, audio_directory: Path) -> None:
        self._db_path = db_path
        self._audio_directory = audio_directory
        self._lock = threading.RLock()
        self._subscribers: dict[str, set[asyncio.Queue[JobEvent]]] = {}
        db_path.parent.mkdir(parents=True, exist_ok=True)
        audio_directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.row_factory = sqlite3.Row
        return connection

    def close(self) -> None:
        with contextlib.suppress(Exception), self._connect() as connection:
            connection.close()

    def create_job(
        self,
        *,
        job_id: str,
        url: str,
        canonical_url: str,
        video_id: str,
        mode: AnalysisMode,
    ) -> Job:
        now = time.time()
        with self._connect() as connection, self._lock:
            connection.execute(
                """
                INSERT INTO jobs
                    (id, url, canonical_url, video_id, mode, status, progress, message,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    url,
                    canonical_url,
                    video_id,
                    mode.value,
                    JobStatus.queued.value,
                    0,
                    "Queued",
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                job_id=job_id,
                status=JobStatus.queued,
                progress=0,
                message="Queued",
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Job:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_job(row)

    def find_job(self, job_id: str) -> Job | None:
        try:
            return self.get_job(job_id)
        except KeyError:
            return None

    def list_jobs(self) -> list[Job]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_resumable_jobs(self) -> list[Job]:
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders})",
                tuple(status.value for status in ACTIVE_STATUSES),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def update_job_state(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        progress: int | None = None,
        message: str | None = None,
        cache_hit: bool | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[Any] = []
        if status is not None:
            assignments.append("status = ?")
            values.append(status.value)
        if progress is not None:
            assignments.append("progress = ?")
            values.append(max(0, min(100, progress)))
        if message is not None:
            assignments.append("message = ?")
            values.append(message)
        if cache_hit is not None:
            assignments.append("cache_hit = ?")
            values.append(1 if cache_hit else 0)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        values.append(time.time())
        values.append(job_id)
        with self._connect() as connection, self._lock:
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if status is not None:
                self._append_event(
                    connection,
                    job_id=job_id,
                    status=status,
                    progress=progress or 0,
                    message=message or "",
                )

    def complete_job(
        self,
        job_id: str,
        metadata: VideoMetadata,
        tracks: list[Track],
    ) -> None:
        event = JobEvent(
            status=JobStatus.completed,
            progress=100,
            message=f"{len(tracks)} tracks identified",
        )
        with self._connect() as connection, self._lock:
            connection.execute(
                """
                UPDATE jobs SET
                    status = ?,
                    progress = 100,
                    message = ?,
                    metadata_json = ?,
                    tracks_json = ?,
                    error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    JobStatus.completed.value,
                    event.message,
                    metadata.model_dump_json(),
                    json.dumps([track.model_dump() for track in tracks]),
                    time.time(),
                    job_id,
                ),
            )
            self._append_event(
                connection,
                job_id=job_id,
                status=event.status,
                progress=event.progress,
                message=event.message,
            )
        self.broadcast(job_id, event)

    def fail_job(self, job_id: str, error_message: str) -> None:
        with self._connect() as connection, self._lock:
            connection.execute(
                """
                UPDATE jobs SET
                    status = ?,
                    error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    JobStatus.failed.value,
                    error_message,
                    time.time(),
                    job_id,
                ),
            )
            self._append_event(
                connection,
                job_id=job_id,
                status=JobStatus.failed,
                progress=0,
                message=error_message,
            )

    def cancel_job(self, job_id: str) -> bool:
        with self._connect() as connection, self._lock:
            cursor = connection.execute(
                """
                UPDATE jobs SET
                    status = ?,
                    message = ?,
                    updated_at = ?
                WHERE id = ? AND status NOT IN (?, ?, ?)
                """,
                (
                    JobStatus.cancelled.value,
                    "Cancelled by user",
                    time.time(),
                    job_id,
                    JobStatus.completed.value,
                    JobStatus.failed.value,
                    JobStatus.cancelled.value,
                ),
            )
            changed = cursor.rowcount > 0
            if changed:
                self._append_event(
                    connection,
                    job_id=job_id,
                    status=JobStatus.cancelled,
                    progress=0,
                    message="Cancelled by user",
                )
        if changed:
            self.broadcast(
                job_id,
                JobEvent(status=JobStatus.cancelled, progress=0, message="Cancelled"),
            )
        return changed

    def delete_job(self, job_id: str) -> bool:
        with self._connect() as connection, self._lock:
            cursor = connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            connection.execute(
                "DELETE FROM section_recognitions WHERE job_id = ?", (job_id,)
            )
            connection.execute(
                "DELETE FROM job_events WHERE job_id = ?", (job_id,)
            )
            return cursor.rowcount > 0

    def get_audio_path(self, video_id: str) -> Path | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM audio_cache WHERE video_id = ?", (video_id,)
            ).fetchone()
        if row is None:
            return None
        file_path = Path(row["file_path"])
        if not file_path.is_file():
            return None
        connection = self._connect()
        connection.execute(
            "UPDATE audio_cache SET last_accessed_at = ? WHERE video_id = ?",
            (time.time(), video_id),
        )
        connection.close()
        return file_path

    def store_audio(
        self,
        video_id: str,
        source_path: Path,
        duration_seconds: int,
    ) -> Path:
        target = self._audio_directory / f"{video_id}{source_path.suffix.lower() or '.m4a'}"
        if target != source_path:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source_path.read_bytes())
        size = target.stat().st_size
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audio_cache
                    (video_id, file_path, duration_seconds, bytes, created_at, last_accessed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    file_path = excluded.file_path,
                    duration_seconds = excluded.duration_seconds,
                    bytes = excluded.bytes,
                    last_accessed_at = excluded.last_accessed_at
                """,
                (video_id, str(target), duration_seconds, size, now, now),
            )
        return target

    def get_fingerprint(self, sample_hash: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM fingerprint_cache WHERE sample_hash = ?",
                (sample_hash,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["result_json"])
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        connection = self._connect()
        connection.execute(
            "UPDATE fingerprint_cache SET last_accessed_at = ? WHERE sample_hash = ?",
            (time.time(), sample_hash),
        )
        connection.close()
        return payload

    def store_fingerprint(self, sample_hash: str, result: dict[str, Any]) -> None:
        connection = self._connect()
        connection.execute(
            """
            INSERT INTO fingerprint_cache (sample_hash, result_json, created_at, last_accessed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(sample_hash) DO UPDATE SET
                result_json = excluded.result_json,
                last_accessed_at = excluded.last_accessed_at
            """,
            (sample_hash, json.dumps(result), time.time(), time.time()),
        )
        connection.close()

    def section_recognitions(self, job_id: str) -> list[SectionRecognition]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sample_position, fingerprint, artist, title, mix,
                       offset_seconds, cover_url, track_url
                FROM section_recognitions
                WHERE job_id = ?
                ORDER BY sample_position
                """,
                (job_id,),
            ).fetchall()
        return [
            SectionRecognition(
                sample_position=row["sample_position"],
                fingerprint=row["fingerprint"],
                artist=row["artist"],
                title=row["title"],
                mix=row["mix"],
                offset=row["offset_seconds"],
                cover_url=row["cover_url"],
                track_url=row["track_url"],
            )
            for row in rows
        ]

    def save_section_recognition(
        self,
        job_id: str,
        sample_position: int,
        sample_hash: str,
        recognition: SectionRecognition,
    ) -> None:
        with self._connect() as connection, self._lock:
            connection.execute(
                """
                INSERT INTO section_recognitions
                    (job_id, sample_position, sample_hash, fingerprint,
                     artist, title, mix, offset_seconds, cover_url, track_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, sample_position) DO UPDATE SET
                    sample_hash = excluded.sample_hash,
                    fingerprint = excluded.fingerprint,
                    artist = excluded.artist,
                    title = excluded.title,
                    mix = excluded.mix,
                    offset_seconds = excluded.offset_seconds,
                    cover_url = excluded.cover_url,
                    track_url = excluded.track_url
                """,
                (
                    job_id,
                    sample_position,
                    sample_hash,
                    recognition.fingerprint,
                    recognition.artist,
                    recognition.title,
                    recognition.mix,
                    recognition.offset,
                    recognition.cover_url,
                    recognition.track_url,
                    time.time(),
                ),
            )

    def replace_tracks_in_range(
        self,
        job_id: str,
        start_seconds: int,
        end_seconds: int,
        tracks: list[Track],
    ) -> None:
        job = self.get_job(job_id)
        kept = [
            track for track in job.tracks
            if track.timestamp_seconds < start_seconds
            or track.timestamp_seconds > end_seconds
        ]
        merged = sorted(kept + tracks, key=lambda track: track.timestamp_seconds)
        renumbered = [
            track.model_copy(update={"position": index + 1})
            for index, track in enumerate(merged)
        ]
        with self._connect() as connection, self._lock:
            connection.execute(
                "UPDATE jobs SET tracks_json = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps([track.model_dump() for track in renumbered]),
                    time.time(),
                    job_id,
                ),
            )

    def counts(self) -> dict[str, int]:
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        with self._connect() as connection:
            active = connection.execute(
                f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({placeholders})",
                tuple(status.value for status in ACTIVE_STATUSES),
            ).fetchone()["n"]
            queued = connection.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status = ?",
                (JobStatus.queued.value,),
            ).fetchone()["n"]
            cache_files = connection.execute(
                "SELECT COUNT(*) AS n FROM audio_cache"
            ).fetchone()["n"]
            fingerprints = connection.execute(
                "SELECT COUNT(*) AS n FROM fingerprint_cache"
            ).fetchone()["n"]
        return {
            "active": int(active),
            "queued": int(queued),
            "cache_files": int(cache_files),
            "fingerprints": int(fingerprints),
        }

    def subscribe(self, job_id: str) -> asyncio.Queue[JobEvent]:
        queue: asyncio.Queue[JobEvent] = asyncio.Queue(maxsize=64)
        self._subscribers.setdefault(job_id, set()).add(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue[JobEvent]) -> None:
        if job_id in self._subscribers:
            self._subscribers[job_id].discard(queue)
            if not self._subscribers[job_id]:
                self._subscribers.pop(job_id, None)

    def unsubscribe_all(self, job_id: str) -> None:
        self._subscribers.pop(job_id, None)

    def broadcast(self, job_id: str, event: JobEvent) -> None:
        for queue in list(self._subscribers.get(job_id, ())):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    def set_rescan_section(
        self,
        job_id: str,
        start_seconds: int,
        end_seconds: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO rescan_requests (job_id, start_seconds, end_seconds, requested_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    start_seconds = excluded.start_seconds,
                    end_seconds = excluded.end_seconds,
                    requested_at = excluded.requested_at
                """,
                (job_id, start_seconds, end_seconds, time.time()),
            )

    def pop_rescan_section(self, job_id: str) -> tuple[int, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT start_seconds, end_seconds FROM rescan_requests WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "DELETE FROM rescan_requests WHERE job_id = ?",
                (job_id,),
            )
        return int(row["start_seconds"]), int(row["end_seconds"])

    def tail_events(self, job_id: str, limit: int = 20) -> list[JobEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, progress, message FROM job_events
                WHERE job_id = ? ORDER BY id DESC LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
        return [
            JobEvent(
                status=JobStatus(row["status"]),
                progress=row["progress"],
                message=row["message"],
            )
            for row in reversed(rows)
        ]

    @staticmethod
    def hash_sample(file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        status: JobStatus,
        progress: int,
        message: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_events (job_id, created_at, status, progress, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, time.time(), status.value, progress, message),
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        metadata: VideoMetadata | None = None
        if row["metadata_json"]:
            metadata = VideoMetadata.model_validate_json(row["metadata_json"])
        tracks: list[Track] = []
        if row["tracks_json"]:
            tracks_data = json.loads(row["tracks_json"])
            tracks = [Track.model_validate(item) for item in tracks_data]
        return Job(
            id=row["id"],
            url=row["url"],
            canonical_url=row["canonical_url"],
            video_id=row["video_id"],
            mode=AnalysisMode(row["mode"]),
            status=JobStatus(row["status"]),
            progress=row["progress"],
            message=row["message"],
            metadata=metadata,
            tracks=tracks,
            error=row["error"],
            cache_hit=bool(row["cache_hit"]),
            created_at=datetime.fromtimestamp(row["created_at"], tz=timezone.utc),
            updated_at=datetime.fromtimestamp(row["updated_at"], tz=timezone.utc),
        )


def is_terminal(status: JobStatus) -> bool:
    return status in TERMINAL_STATUSES


def iter_active_statuses() -> Iterable[JobStatus]:
    return iter(ACTIVE_STATUSES)
