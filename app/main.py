from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from app.models import (
    CreateJobRequest,
    HealthResponse,
    Job,
    JobEvent,
    JobStatus,
    RescanRequest,
    VideoMetadata,
)
from app.recognizer import TracklistAnalyzer
from app.store import Store
from app.youtube import InvalidYouTubeUrl, canonical_youtube_url

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("trackid")

DATA_DIRECTORY = Path(os.getenv("TRACKID_DATA", "./data"))
DB_PATH = DATA_DIRECTORY / "trackid.db"
AUDIO_DIRECTORY = DATA_DIRECTORY / "audio"
store = Store(DB_PATH, AUDIO_DIRECTORY)
analyzer = TracklistAnalyzer(store)

queue: asyncio.Queue[str] = asyncio.Queue()
cancel_events: dict[str, asyncio.Event] = {}
cancel_tasks: dict[str, asyncio.Task[None]] = {}
inflight: set[str] = set()
_workers: list[asyncio.Task[None]] = []
app = FastAPI(title="TrackID", version="0.2.0", docs_url="/api/docs", redoc_url=None)


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    counts = store.counts()
    return HealthResponse(
        status="ok",
        version=app.version,
        active_jobs=len(inflight),
        queued_jobs=counts["queued"],
        cache_files=counts["cache_files"],
    )


@app.post("/api/jobs", response_model=Job, status_code=202)
async def create_job(request: CreateJobRequest) -> Job:
    try:
        canonical_url = canonical_youtube_url(request.url)
    except InvalidYouTubeUrl as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    video_id = canonical_url.rsplit("=", 1)[-1]
    job_id = secrets.token_urlsafe(9)
    job = store.create_job(
        job_id=job_id,
        url=request.url,
        canonical_url=canonical_url,
        video_id=video_id,
        mode=request.mode,
    )
    await queue.put(job_id)
    return job


@app.get("/api/jobs", response_model=list[Job])
async def list_jobs() -> list[Job]:
    return store.list_jobs()


@app.get("/api/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str) -> Job:
    job = store.find_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    job = store.find_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    subscriber = store.subscribe(job_id)

    async def event_stream() -> AsyncIterator[str]:
        try:
            for event in store.tail_events(job_id, limit=50):
                yield _format_sse(event)
            yield ": ping\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(subscriber.get(), timeout=15.0)
                    yield _format_sse(event)
                    if event.status in {
                        JobStatus.completed,
                        JobStatus.failed,
                        JobStatus.cancelled,
                    }:
                        break
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            store.unsubscribe(job_id, subscriber)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> Response:
    job = store.find_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    cancel_event = cancel_events.get(job_id)
    if cancel_event is not None:
        cancel_event.set()
    cancelled = store.cancel_job(job_id)
    store.delete_job(job_id)
    store.unsubscribe_all(job_id)
    return JSONResponse({"status": "ok" if cancelled else "deleted", "deleted": True})


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> JSONResponse:
    task = cancel_tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
    cancel_event = cancel_events.get(job_id)
    if cancel_event is not None:
        cancel_event.set()
    if not store.cancel_job(job_id):
        raise HTTPException(status_code=409, detail="Job is no longer cancellable")
    return JSONResponse({"status": "cancelled"})


@app.post("/api/jobs/{job_id}/rescan", response_model=Job)
async def rescan_job(job_id: str, request: RescanRequest) -> Job:
    job = store.find_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if request.end <= request.start:
        raise HTTPException(status_code=422, detail="end must be greater than start")
    if job.metadata is None:
        raise HTTPException(status_code=409, detail="Job has no metadata")
    if request.end > job.metadata.duration_seconds:
        raise HTTPException(status_code=422, detail="Section exceeds video duration")
    store.set_rescan_section(job_id, request.start, request.end)
    store.update_job_state(job_id, status=JobStatus.queued, progress=0, message="Rescan queued")
    await queue.put(job_id)
    return store.get_job(job_id)


@app.get("/api/jobs/{job_id}/export")
async def export_job(
    job_id: str,
    format: str = Query("json", pattern="^(json|m3u|csv|txt)$"),
) -> Response:
    job = store.find_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.metadata is None:
        raise HTTPException(status_code=409, detail="Job has no metadata yet")
    metadata = job.metadata
    payload = serialize_job(job)
    if format == "json":
        return JSONResponse(payload)
    body, media_type, filename = render_export(format, job, payload, metadata)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=body, media_type=media_type, headers=headers)


async def worker() -> None:
    while True:
        job_id = await queue.get()
        if job_id not in inflight:
            await _run_job(job_id)
        queue.task_done()


async def _run_job(job_id: str) -> None:
    job = store.find_job(job_id)
    if job is None:
        return
    cancel_event = asyncio.Event()
    cancel_events[job_id] = cancel_event
    inflight.add(job_id)
    analysis_task = asyncio.current_task()
    if analysis_task is not None:
        cancel_tasks[job_id] = analysis_task
    try:
        section = store.pop_rescan_section(job_id)
        if section is not None:
            await _run_rescan(job, section, cancel_event)
        else:
            await _run_full(job, cancel_event)
    except asyncio.CancelledError:
        logger.info("Job %s cancelled", job_id)
        store.update_job_state(
            job_id, status=JobStatus.cancelled, progress=0, message="Cancelled"
        )
        store.broadcast(
            job_id,
            JobEvent(status=JobStatus.cancelled, progress=0, message="Cancelled"),
        )
    except Exception as error:
        logger.exception("Job %s crashed", job_id)
        message = public_error(error)
        store.fail_job(job_id, message)
        store.broadcast(
            job_id, JobEvent(status=JobStatus.failed, progress=0, message=message)
        )
    finally:
        cancel_events.pop(job_id, None)
        cancel_tasks.pop(job_id, None)
        inflight.discard(job_id)


async def _run_full(job: Job, cancel_event: asyncio.Event) -> None:
    def update(status: JobStatus, progress: int, message: str) -> None:
        store.update_job_state(job.id, status=status, progress=progress, message=message)
        store.broadcast(job.id, JobEvent(status=status, progress=progress, message=message))

    def is_cancelled() -> bool:
        return cancel_event.is_set()

    await analyzer.analyze(
        job_id=job.id,
        url=job.url,
        mode=job.mode,
        progress=update,
        is_cancelled=is_cancelled,
    )


async def _run_rescan(
    job: Job,
    section: tuple[int, int],
    cancel_event: asyncio.Event,
) -> None:
    start, end = section

    def update(status: JobStatus, progress: int, message: str) -> None:
        store.update_job_state(job.id, status=status, progress=progress, message=message)
        store.broadcast(job.id, JobEvent(status=status, progress=progress, message=message))

    def is_cancelled() -> bool:
        return cancel_event.is_set()

    await analyzer.rescan_section(
        job_id=job.id,
        start_seconds=start,
        end_seconds=end,
        progress=update,
        is_cancelled=is_cancelled,
    )


def _format_sse(event: JobEvent) -> str:
    payload = {
        "status": event.status.value,
        "progress": event.progress,
        "message": event.message,
    }
    return (
        f"event: {event.status.value}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def public_error(error: Exception) -> str:
    if isinstance(error, (ValueError, RuntimeError, InvalidYouTubeUrl)):
        message = str(error)
    else:
        message = "Could not process this YouTube session"
    return re.sub(r"\x1b\[[0-9;]*m", "", message).strip()[:400]


def serialize_job(job: Job) -> dict[str, Any]:
    return job.model_dump(mode="json")


def render_export(
    format: str,
    job: Job,
    payload: dict[str, Any],
    metadata: VideoMetadata,
) -> tuple[str, str, str]:
    safe_title = (
        re.sub(r"[^A-Za-z0-9_-]+", "_", metadata.title)
        .strip("_")
        or "tracklist"
    )
    if format == "csv":
        rows = ["#", "timestamp", "seconds", "artist", "title", "mix", "source"]
        for track in job.tracks:
            rows.append(
                ",".join(
                    [
                        str(track.position),
                        track.timestamp,
                        str(track.timestamp_seconds),
                        _csv_escape(track.artist),
                        _csv_escape(track.title),
                        _csv_escape(track.mix),
                        track.source,
                    ]
                )
            )
        return (
            "\ufeff" + "\n".join(rows),
            "text/csv; charset=utf-8",
            f"{safe_title}.csv",
        )
    if format == "txt":
        lines = [
            metadata.title,
            f"Channel: {metadata.channel or 'Unknown'}",
            f"Duration: {metadata.duration}",
            "",
        ]
        for track in job.tracks:
            artist = track.artist or "Unknown artist"
            mix = f" ({track.mix})" if track.mix else ""
            lines.append(f"{track.timestamp}  {artist} — {track.title}{mix}")
        return ("\n".join(lines), "text/plain; charset=utf-8", f"{safe_title}.txt")
    if format == "m3u":
        lines = [
            "#EXTM3U",
            f"# {metadata.title}",
            f"# {metadata.channel or ''}",
            "",
        ]
        previous_seconds = 0
        for track in job.tracks:
            duration = max(0, track.timestamp_seconds - previous_seconds)
            previous_seconds = track.timestamp_seconds
            label = f"{track.artist or 'Unknown'} - {track.title}"
            if track.mix:
                label = f"{label} ({track.mix})"
            lines.append(f"#EXTINF:{duration},{label}")
            lines.append(f"{metadata.youtube_url}&t={track.timestamp_seconds}s")
        return ("\n".join(lines), "audio/x-mpegurl", f"{safe_title}.m3u")
    raise ValueError(f"Unsupported export format: {format}")


def _csv_escape(value: str | None) -> str:
    if value is None:
        return ""
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


@app.on_event("startup")
async def startup() -> None:
    max_workers = max(0, int(os.getenv("TRACKID_MAX_WORKERS", "1")))
    for index in range(max_workers):
        _workers.append(asyncio.create_task(worker(), name=f"trackid-worker-{index}"))
    for job in store.list_resumable_jobs():
        if job.id not in inflight:
            await queue.put(job.id)
            logger.info("Re-queued resumed job %s from previous run", job.id)


@app.on_event("shutdown")
async def shutdown() -> None:
    for cancel_event in cancel_events.values():
        cancel_event.set()
    for worker_task in _workers:
        worker_task.cancel()
    for worker_task in _workers:
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task


static_directory = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=static_directory, html=True), name="static")


@app.exception_handler(404)
async def not_found_handler(_: Request, __: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Resource not found"})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    path = static_directory / "favicon.svg"
    if path.is_file():
        return FileResponse(path)
    return PlainTextResponse("", status_code=204)
