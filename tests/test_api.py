from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    JobEvent,
    JobStatus,
    Track,
    VideoMetadata,
)
from app.store import Store


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACKID_MAX_WORKERS", "0")
    yield


@pytest.fixture
def app_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fresh = Store(tmp_path / "trackid.db", tmp_path / "audio")
    monkeypatch.setattr("app.main.store", fresh)
    class _NoopAnalyzer:
        async def analyze(self, **_kwargs: object) -> dict[str, object]:
            return {"source": "shazam"}

        async def rescan_section(self, **_kwargs: object) -> int:
            return 0

    monkeypatch.setattr("app.main.analyzer", _NoopAnalyzer())
    with TestClient(app) as client:
        yield client, fresh
    fresh.close()


def test_health_reports_active_jobs(app_state) -> None:
    client, _ = app_state
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == app.version
    assert body["active_jobs"] == 0


def test_frontend_and_favicon(app_state) -> None:
    client, _ = app_state
    home = client.get("/")
    favicon = client.get("/favicon.svg")
    assert home.status_code == 200
    assert "From a YouTube set" in home.text
    assert favicon.status_code in {200, 204}


def test_rejects_invalid_url(app_state) -> None:
    client, _ = app_state
    response = client.post("/api/jobs", json={"url": "https://example.com"})
    assert response.status_code == 422
    assert response.json()["detail"] == "Introduce una URL válida de YouTube"


def test_create_and_complete_job(app_state) -> None:
    client, store = app_state
    response = client.post(
        "/api/jobs",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "mode": "thorough"},
    )
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    metadata = VideoMetadata(
        title="Test set",
        duration_seconds=3600,
        duration="1:00:00",
        youtube_url="https://youtu.be/dQw4w9WgXcQ",
    )
    tracks = [
        Track(
            position=1,
            timestamp_seconds=0,
            timestamp="00:00",
            artist="Artist",
            title="Track",
            source="youtube",
        )
    ]
    store.complete_job(job["id"], metadata, tracks)
    fetched = client.get(f"/api/jobs/{job['id']}").json()
    assert fetched["status"] == "completed"
    assert fetched["tracks"][0]["artist"] == "Artist"


def test_cancel_job(app_state) -> None:
    client, _ = app_state
    response = client.post(
        "/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
    )
    job_id = response.json()["id"]
    cancel = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json() == {"status": "cancelled"}


def test_export_formats(app_state) -> None:
    client, store = app_state
    response = client.post(
        "/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
    )
    job_id = response.json()["id"]
    store.complete_job(
        job_id,
        VideoMetadata(
            title="Demo Set",
            duration_seconds=200,
            duration="03:20",
            youtube_url="https://youtu.be/dQw4w9WgXcQ",
        ),
        [
            Track(
                position=1,
                timestamp_seconds=0,
                timestamp="00:00",
                artist="Artist A",
                title="Intro",
                source="shazam",
            ),
            Track(
                position=2,
                timestamp_seconds=120,
                timestamp="02:00",
                artist="Artist B",
                title="Peak",
                mix="Original Mix",
                source="shazam",
            ),
        ],
    )
    json_response = client.get(f"/api/jobs/{job_id}/export?format=json")
    assert json_response.status_code == 200
    assert json_response.json()["tracks"][0]["artist"] == "Artist A"
    m3u_response = client.get(f"/api/jobs/{job_id}/export?format=m3u")
    assert m3u_response.status_code == 200
    assert "#EXTM3U" in m3u_response.text
    assert "Artist B" in m3u_response.text
    csv_response = client.get(f"/api/jobs/{job_id}/export?format=csv")
    assert csv_response.status_code == 200
    assert "Original Mix" in csv_response.text


def test_rescan_request(app_state) -> None:
    client, store = app_state
    response = client.post(
        "/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
    )
    job_id = response.json()["id"]
    store.complete_job(
        job_id,
        VideoMetadata(
            title="Demo",
            duration_seconds=600,
            duration="10:00",
            youtube_url="https://youtu.be/dQw4w9WgXcQ",
        ),
        [],
    )
    rescan = client.post(
        f"/api/jobs/{job_id}/rescan", json={"start": 30, "end": 90}
    )
    assert rescan.status_code == 200
    assert rescan.json()["status"] == "queued"


def test_delete_job(app_state) -> None:
    client, _ = app_state
    response = client.post(
        "/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
    )
    job_id = response.json()["id"]
    delete = client.delete(f"/api/jobs/{job_id}")
    assert delete.status_code == 200
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_broadcast_event_after_completion(app_state) -> None:
    client, store = app_state
    response = client.post(
        "/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
    )
    job_id = response.json()["id"]
    subscriber = store.subscribe(job_id)
    metadata = VideoMetadata(
        title="x", duration_seconds=10, duration="00:10", youtube_url="x"
    )
    store.complete_job(job_id, metadata, [])
    event: JobEvent = subscriber.get_nowait()
    assert event.status == JobStatus.completed
    assert event.progress == 100
    store.unsubscribe(job_id, subscriber)
