from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shazamio import Shazam

from app.audio import create_sample, detect_transitions
from app.models import (
    AnalysisMode,
    JobStatus,
    Track,
    VideoMetadata,
)
from app.store import SectionRecognition, Store
from app.youtube import (
    best_embedded_tracklist,
    canonical_youtube_url,
    download_audio,
    format_timestamp,
    inspect_video,
)

logger = logging.getLogger("trackid.recognizer")

ProgressCallback = Callable[[JobStatus, int, str], None]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True)
class RecognitionResult:
    sample_position: int
    fingerprint: str
    artist: str | None
    title: str
    mix: str | None
    offset: float | None
    cover_url: str | None
    track_url: str | None


@dataclass(frozen=True)
class ModeProfile:
    sample_interval: int
    enable_transitions: bool
    max_samples: int
    concurrency: int


PROFILES: dict[AnalysisMode, ModeProfile] = {
    AnalysisMode.quick: ModeProfile(75, False, 80, 4),
    AnalysisMode.thorough: ModeProfile(35, True, 320, 2),
}


MIX_PATTERN = re.compile(
    r"\s*[\(\[\{](?P<mix>[^\)\]\}]{1,40})[\)\]\}]\s*$"
)
EXPLICIT_PREFIX_PATTERN = re.compile(r"^\s*(?P<value>explicit|clean)\s*[:\-]\s*", re.IGNORECASE)
VERSION_KEYWORDS = (
    "original mix",
    "extended mix",
    "radio edit",
    "club mix",
    "dub mix",
    "remix",
    "edit",
    "remaster",
    "instrumental",
)


class TracklistAnalyzer:
    def __init__(self, store: Store) -> None:
        self._store = store
        self.max_duration = int(os.getenv("TRACKID_MAX_DURATION", "14400"))

    async def analyze(
        self,
        *,
        job_id: str,
        url: str,
        mode: AnalysisMode,
        progress: ProgressCallback,
        is_cancelled: CancelCallback,
    ) -> dict[str, object]:
        canonical_url = canonical_youtube_url(url)
        video_id = canonical_url.rsplit("=", 1)[-1]
        self._store.update_job_state(
            job_id,
            status=JobStatus.inspecting,
            progress=5,
            message="Reading video metadata",
        )
        info = await asyncio.to_thread(inspect_video, canonical_url)
        if is_cancelled():
            raise asyncio.CancelledError()

        duration = round(float(info.get("duration") or 0))
        if duration <= 0:
            raise ValueError("The video must be published and have a known duration")
        if duration > self.max_duration:
            raise ValueError(
                f"Session exceeds the configured maximum of {self.max_duration // 3600} hours"
            )

        metadata = VideoMetadata(
            title=str(info.get("title") or "DJ Session"),
            channel=_optional_text(info.get("channel") or info.get("uploader")),
            duration_seconds=duration,
            duration=format_timestamp(duration),
            thumbnail_url=_optional_text(info.get("thumbnail")),
            youtube_url=canonical_url,
        )

        embedded = best_embedded_tracklist(info)
        profile = PROFILES[mode]
        expected_minimum = max(3, math.ceil(duration / 900))
        if len(embedded) >= expected_minimum:
            self._store.update_job_state(
                job_id,
                status=JobStatus.completed,
                progress=100,
                message="Tracklist extracted from YouTube",
            )
            self._store.complete_job(job_id, metadata, embedded)
            return {"source": "youtube"}

        self._store.update_job_state(
            job_id,
            status=JobStatus.downloading,
            progress=15,
            message="Downloading session audio",
        )
        audio_path, cache_hit = await asyncio.to_thread(
            self._acquire_audio, video_id, canonical_url, duration
        )
        if is_cancelled():
            raise asyncio.CancelledError()
        self._store.update_job_state(
            job_id,
            status=JobStatus.recognizing,
            progress=28,
            message="Sampling audio",
            cache_hit=cache_hit,
        )

        recognitions = await self._recognize_audio(
            audio_path=audio_path,
            duration=duration,
            mode=mode,
            progress=progress,
            is_cancelled=is_cancelled,
            job_id=job_id,
        )
        if is_cancelled():
            raise asyncio.CancelledError()

        tracks = build_tracklist(
            recognitions,
            sample_interval=profile.sample_interval,
        )
        if len(embedded) > len(tracks):
            tracks = embedded
        if not tracks:
            raise RuntimeError(
                "No tracks were recognized. The session might be too noisy or contain unknown IDs"
            )
        self._store.complete_job(job_id, metadata, tracks)
        return {"source": "shazam", "tracks": len(tracks)}

    async def rescan_section(
        self,
        *,
        job_id: str,
        start_seconds: int,
        end_seconds: int,
        progress: ProgressCallback,
        is_cancelled: CancelCallback,
    ) -> int:
        job = self._store.get_job(job_id)
        if job.video_id is None:
            raise ValueError("Job has no video metadata")
        cached = self._store.get_audio_path(job.video_id)
        if cached is None:
            raise RuntimeError(
                "Audio cache missing. Rescan requires a previous successful download"
            )
        self._store.update_job_state(
            job_id,
            status=JobStatus.rescanning,
            progress=5,
            message=(
                f"Rescanning {format_timestamp(start_seconds)}"
                f" – {format_timestamp(end_seconds)}"
            ),
        )
        recognitions = await self._recognize_audio(
            audio_path=cached,
            duration=end_seconds,
            mode=AnalysisMode.thorough,
            progress=progress,
            is_cancelled=is_cancelled,
            job_id=job_id,
            start_override=start_seconds,
            end_override=end_seconds,
        )
        if is_cancelled():
            raise asyncio.CancelledError()
        tracks = build_tracklist(
            recognitions,
            sample_interval=PROFILES[AnalysisMode.thorough].sample_interval,
        )
        self._store.replace_tracks_in_range(job_id, start_seconds, end_seconds, tracks)
        self._store.update_job_state(
            job_id,
            status=JobStatus.completed,
            progress=100,
            message=f"Rescanned {len(tracks)} tracks",
        )
        return len(tracks)

    def _acquire_audio(
        self,
        video_id: str,
        canonical_url: str,
        duration: int,
    ) -> tuple[Path, bool]:
        cached = self._store.get_audio_path(video_id)
        if cached is not None:
            return cached, True
        with tempfile.TemporaryDirectory(prefix="trackid-") as temporary_directory:
            audio_path = download_audio(canonical_url, Path(temporary_directory))
            stored = self._store.store_audio(video_id, audio_path, duration)
            return stored, False

    async def _recognize_audio(
        self,
        *,
        audio_path: Path,
        duration: int,
        mode: AnalysisMode,
        progress: ProgressCallback,
        is_cancelled: CancelCallback,
        job_id: str,
        start_override: int | None = None,
        end_override: int | None = None,
    ) -> list[RecognitionResult]:
        profile = PROFILES[mode]
        interval = max(profile.sample_interval, math.ceil(duration / profile.max_samples))
        start = start_override if start_override is not None else min(15, duration - 16)
        end = end_override if end_override is not None else duration
        positions = list(range(max(0, start), max(start + 1, end), interval))
        last = end - 16
        if last > 0 and (not positions or positions[-1] != last):
            positions.append(last)

        if profile.enable_transitions and start_override is None:
            try:
                progress(JobStatus.recognizing, 30, "Detecting transitions")
                transitions = await asyncio.to_thread(
                    detect_transitions, audio_path
                )
                for transition in transitions:
                    if 30 < transition.position_seconds < duration - 16:
                        positions.append(int(transition.position_seconds))
            except OSError as error:
                logger.warning("Silencedetect failed: %s", error)
        positions = sorted(
            {
                max(0, position)
                for position in positions
                if 0 <= position < duration - 10
            }
        )

        if not positions:
            return []

        semaphore = asyncio.Semaphore(profile.concurrency)
        completed_counter = 0
        completed_lock = asyncio.Lock()
        shazam = Shazam()

        async def recognize_position(position: int) -> RecognitionResult | None:
            nonlocal completed_counter
            if is_cancelled():
                return None
            async with semaphore:
                sample_path = audio_path.parent / f"sample-{position}.mp3"
                try:
                    await asyncio.to_thread(
                        create_sample,
                        audio_path,
                        sample_path,
                        position,
                    )
                    if is_cancelled():
                        return None
                    sample_hash = await asyncio.to_thread(Store.hash_sample, sample_path)
                    cached_result = self._store.get_fingerprint(sample_hash)
                    if cached_result is not None:
                        recognition = parse_recognition(cached_result, position)
                    else:
                        raw_result = await shazam.recognize(str(sample_path))
                        await asyncio.to_thread(
                            self._store.store_fingerprint, sample_hash, raw_result
                        )
                        recognition = parse_recognition(raw_result, position)
                    if recognition is not None:
                        section = SectionRecognition(
                            sample_position=position,
                            fingerprint=recognition.fingerprint,
                            artist=recognition.artist,
                            title=recognition.title,
                            mix=recognition.mix,
                            offset=recognition.offset,
                            cover_url=recognition.cover_url,
                            track_url=recognition.track_url,
                        )
                        self._store.save_section_recognition(
                            job_id, position, sample_hash, section
                        )
                    return recognition
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    KeyError,
                    asyncio.TimeoutError,
                    asyncio.CancelledError,
                    subprocess.SubprocessError,
                ):
                    return None
                finally:
                    sample_path.unlink(missing_ok=True)
                    async with completed_lock:
                        completed_counter += 1
                        percentage = 32 + round(
                            (completed_counter / len(positions)) * 63
                        )
                        progress(
                            JobStatus.recognizing,
                            min(95, percentage),
                            f"Recognized {completed_counter} of {len(positions)} samples",
                        )

        results = await asyncio.gather(
            *(recognize_position(position) for position in positions)
        )
        return sorted(
            (result for result in results if result),
            key=lambda item: item.sample_position,
        )


def build_tracklist(
    recognitions: list[RecognitionResult], *, sample_interval: int
) -> list[Track]:
    if not recognitions:
        return []
    tracks: list[Track] = []
    previous: RecognitionResult | None = None
    active_fingerprint: str | None = None

    for recognition in recognitions:
        if recognition.fingerprint == active_fingerprint:
            previous = recognition
            continue
        if previous is None:
            estimated = _estimated_start(recognition)
            fallback = max(0, recognition.sample_position - sample_interval // 2)
            start = estimated if estimated is not None else fallback
        else:
            midpoint = round(
                (previous.sample_position + recognition.sample_position) / 2
            )
            estimated = _estimated_start(recognition)
            start = (
                estimated
                if estimated is not None
                and previous.sample_position <= estimated <= recognition.sample_position
                else midpoint
            )
        start = max(0, start)
        tracks.append(
            Track(
                position=len(tracks) + 1,
                timestamp_seconds=start,
                timestamp=format_timestamp(start),
                artist=recognition.artist,
                title=recognition.title,
                mix=recognition.mix,
                cover_url=recognition.cover_url,
                track_url=recognition.track_url,
                source="shazam",
            )
        )
        active_fingerprint = recognition.fingerprint
        previous = recognition
    return tracks


def parse_recognition(
    result: dict[str, Any],
    sample_position: int,
) -> RecognitionResult | None:
    track = result.get("track")
    if not isinstance(track, dict):
        return None
    raw_title = _optional_text(track.get("title"))
    if not raw_title:
        return None
    raw_artist = _optional_text(track.get("subtitle"))
    title, mix = split_mix(raw_title)
    artist = _clean_artist(raw_artist) if raw_artist else None
    fingerprint = _fingerprint(artist, title)
    offset: float | None = None
    matches = result.get("matches")
    if isinstance(matches, list) and matches and isinstance(matches[0], dict):
        raw_offset = matches[0].get("offset")
        try:
            if isinstance(raw_offset, (int, float, str)):
                parsed_offset = float(raw_offset)
                if math.isfinite(parsed_offset) and 0 <= parsed_offset <= 1200:
                    offset = parsed_offset
        except (TypeError, ValueError):
            pass
    images = track.get("images")
    cover_url = (
        _optional_text(images.get("coverart"))
        if isinstance(images, dict)
        else None
    )
    return RecognitionResult(
        sample_position=sample_position,
        fingerprint=fingerprint,
        artist=artist,
        title=title,
        mix=mix,
        offset=offset,
        cover_url=cover_url,
        track_url=_optional_text(track.get("url")),
    )


def split_mix(raw_title: str) -> tuple[str, str | None]:
    cleaned = unicodedata.normalize("NFKC", raw_title).strip()
    match = MIX_PATTERN.search(cleaned)
    if match:
        candidate_mix = match.group("mix").strip()
        title = cleaned[: match.start()].strip(" -–—|:\t")
        if _looks_like_mix(candidate_mix):
            return title or cleaned, candidate_mix
    return cleaned, None


def _looks_like_mix(value: str) -> bool:
    lowered = value.casefold()
    if any(keyword in lowered for keyword in VERSION_KEYWORDS):
        return True
    return bool(re.fullmatch(r"[A-Z][\w &/]+(Edit|Mix)", value))


def _clean_artist(value: str) -> str:
    return EXPLICIT_PREFIX_PATTERN.sub("", value).strip()


def _fingerprint(artist: str | None, title: str) -> str:
    return f"{(artist or '').casefold()}|{title.casefold()}"


def _estimated_start(recognition: RecognitionResult) -> int | None:
    if recognition.offset is None:
        return None
    return max(0, round(recognition.sample_position - recognition.offset))


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
