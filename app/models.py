from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    queued = "queued"
    inspecting = "inspecting"
    downloading = "downloading"
    recognizing = "recognizing"
    rescanning = "rescanning"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL_STATUSES = frozenset({JobStatus.completed, JobStatus.failed, JobStatus.cancelled})
ACTIVE_STATUSES = frozenset(
    status for status in JobStatus if status not in TERMINAL_STATUSES
)


class AnalysisMode(str, Enum):
    quick = "quick"
    thorough = "thorough"


class Track(BaseModel):
    position: int
    timestamp_seconds: int = Field(ge=0)
    timestamp: str
    artist: str | None = None
    title: str
    mix: str | None = None
    cover_url: str | None = None
    track_url: str | None = None
    source: Literal["youtube", "shazam"]


class VideoMetadata(BaseModel):
    title: str
    channel: str | None = None
    duration_seconds: int = Field(ge=0)
    duration: str
    thumbnail_url: str | None = None
    youtube_url: str


class JobEvent(BaseModel):
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    message: str


class Job(BaseModel):
    id: str
    url: str
    canonical_url: str
    video_id: str
    mode: AnalysisMode = AnalysisMode.thorough
    status: JobStatus = JobStatus.queued
    progress: int = Field(default=0, ge=0, le=100)
    message: str = "Queued"
    metadata: VideoMetadata | None = None
    tracks: list[Track] = Field(default_factory=list)
    error: str | None = None
    cache_hit: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CreateJobRequest(BaseModel):
    url: str = Field(min_length=10, max_length=500)
    mode: AnalysisMode = AnalysisMode.thorough


class RescanRequest(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(ge=1)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    active_jobs: int
    queued_jobs: int
    cache_files: int
