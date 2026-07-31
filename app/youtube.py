import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL

from app.models import Track

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
TIMESTAMP_PATTERN = r"(?P<stamp>(?:\d{1,2}:)?\d{1,2}:\d{2})"
PREFIX_TIMESTAMP = re.compile(
    rf"^\s*(?:\d+[.)]\s*)?[\[(]?{TIMESTAMP_PATTERN}[\])]?\s*(?:[-–—|:]\s*)?(?P<label>.+?)\s*$"
)
SUFFIX_TIMESTAMP = re.compile(
    rf"^\s*(?:\d+[.)]\s*)?(?P<label>.+?)\s*(?:[-–—|]\s*)?[\[(]{TIMESTAMP_PATTERN}[\])]\s*$"
)
TITLE_SEPARATOR = re.compile(r"\s+[-–—|]\s+")


class InvalidYouTubeUrl(ValueError):
    pass


def extract_video_id(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in YOUTUBE_HOSTS:
        raise InvalidYouTubeUrl("Introduce una URL válida de YouTube")

    video_id = ""
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    elif parsed.path.rstrip("/") == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"embed", "live", "shorts"}:
            video_id = parts[1]

    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise InvalidYouTubeUrl("La URL no contiene un vídeo válido de YouTube")
    return video_id


def canonical_youtube_url(url: str) -> str:
    return f"https://www.youtube.com/watch?v={extract_video_id(url)}"


def format_timestamp(seconds: int) -> str:
    seconds = max(0, round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def timestamp_to_seconds(value: str) -> int:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def split_artist_title(label: str) -> tuple[str | None, str]:
    cleaned = re.sub(r"^\s*\d+[.)]\s*", "", label).strip(" -–—|\t")
    pieces = TITLE_SEPARATOR.split(cleaned, maxsplit=1)
    if len(pieces) == 2 and all(pieces):
        return pieces[0].strip(), pieces[1].strip()
    return None, cleaned


def parse_description_tracklist(description: str, duration: int) -> list[Track]:
    parsed: list[tuple[int, str]] = []
    for line in description.splitlines():
        match = PREFIX_TIMESTAMP.match(line) or SUFFIX_TIMESTAMP.match(line)
        if not match:
            continue
        label = match.group("label").strip()
        if not label or label.startswith(("http://", "https://")):
            continue
        seconds = timestamp_to_seconds(match.group("stamp"))
        if seconds <= duration + 30:
            parsed.append((seconds, label))
    return _entries_to_tracks(parsed)


def parse_chapter_tracklist(chapters: list[dict[str, Any]] | None) -> list[Track]:
    if not chapters:
        return []
    parsed = [
        (round(float(chapter.get("start_time", 0))), str(chapter.get("title", "")).strip())
        for chapter in chapters
        if str(chapter.get("title", "")).strip()
    ]
    return _entries_to_tracks(parsed)


def best_embedded_tracklist(info: dict[str, Any]) -> list[Track]:
    duration = round(float(info.get("duration") or 0))
    description_tracks = parse_description_tracklist(str(info.get("description") or ""), duration)
    chapter_tracks = parse_chapter_tracklist(info.get("chapters"))
    return max((description_tracks, chapter_tracks), key=len)


def inspect_video(url: str) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 30,
    }
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("No se han podido leer los datos del vídeo")
    return info


def download_audio(url: str, target_directory: Path) -> Path:
    template = str(target_directory / "session.%(ext)s")
    options: dict[str, Any] = {
        "format": "bestaudio[abr<=192]/bestaudio/best",
        "max_filesize": 600 * 1024 * 1024,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": template,
        "quiet": True,
        "socket_timeout": 30,
    }
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
        filename = Path(downloader.prepare_filename(info))
    if not filename.is_file():
        candidates = list(target_directory.glob("session.*"))
        if not candidates:
            raise RuntimeError("No se ha podido descargar el audio")
        return candidates[0]
    return filename


def _entries_to_tracks(entries: list[tuple[int, str]]) -> list[Track]:
    unique: dict[int, str] = {}
    for seconds, label in entries:
        unique.setdefault(seconds, label)
    tracks: list[Track] = []
    for position, (seconds, label) in enumerate(sorted(unique.items()), start=1):
        artist, title = split_artist_title(label)
        tracks.append(
            Track(
                position=position,
                timestamp_seconds=seconds,
                timestamp=format_timestamp(seconds),
                artist=artist,
                title=title,
                source="youtube",
            )
        )
    return tracks
