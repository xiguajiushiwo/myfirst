import io
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.remote_jobs import RemoteJobManager
from app.routers import remote_jobs as remote_router


def image_bytes(color):
    content = io.BytesIO()
    Image.new("RGB", (800, 600), color).save(content, "JPEG")
    return content.getvalue()


def test_remote_job_api_auth_upload_and_result(monkeypatch, tmp_path):
    manager = RemoteJobManager(tmp_path, processor=lambda state, on_stage: {"ok": True, "job_id": state["job_id"]})
    monkeypatch.setattr(remote_router, "manager", manager)
    monkeypatch.setenv("CLIENT_API_KEY", "test-client-key")
    app = FastAPI()
    app.include_router(remote_router.router)
    client = TestClient(app)
    manager.start()
    try:
        assert client.get("/api/remote/health").status_code == 401
        response = client.post(
            "/api/remote/jobs",
            headers={"X-Client-Key": "test-client-key"},
            data={"job_id": "api-job", "station_id": "QC-01", "batch_id": "3",
                  "template_id": "samsung-4up-0808", "current_year": "2026",
                  "threshold": "0.72"},
            files={"front": ("front.jpg", image_bytes("red"), "image/jpeg"),
                   "back": ("back.jpg", image_bytes("blue"), "image/jpeg")},
        )
        assert response.status_code == 200
        assert response.json()["created"] is True
        queued = response.json()["job"]
        assert queued["template_id"] == "samsung-4up-0808"
        assert queued["current_year"] == 2026
        assert queued["threshold"] == 0.72

        deadline = time.time() + 3
        while time.time() < deadline:
            result = client.get("/api/remote/jobs/api-job", headers={"X-Client-Key": "test-client-key"})
            if result.json()["job"]["status"] == "completed":
                break
            time.sleep(0.02)
        assert result.json()["job"]["result"]["job_id"] == "api-job"

        duplicate = client.post(
            "/api/remote/jobs",
            headers={"X-Client-Key": "test-client-key"},
            data={"job_id": "api-job", "station_id": "QC-01"},
            files={"front": ("front.jpg", image_bytes("black"), "image/jpeg"),
                   "back": ("back.jpg", image_bytes("white"), "image/jpeg")},
        )
        assert duplicate.json()["created"] is False
    finally:
        manager.stop()


def test_remote_job_api_rejects_too_dark_images(monkeypatch, tmp_path):
    manager = RemoteJobManager(tmp_path, processor=lambda state, on_stage: {"ok": True})
    monkeypatch.setattr(remote_router, "manager", manager)
    monkeypatch.setenv("CLIENT_API_KEY", "test-client-key")
    app = FastAPI()
    app.include_router(remote_router.router)
    client = TestClient(app)
    manager.start()
    try:
        response = client.post(
            "/api/remote/jobs",
            headers={"X-Client-Key": "test-client-key"},
            data={"job_id": "dark-job", "station_id": "QC-01"},
            files={"front": ("front.jpg", image_bytes("black"), "image/jpeg"),
                   "back": ("back.jpg", image_bytes("black"), "image/jpeg")},
        )

        body = response.json()
        assert response.status_code == 200
        assert body["ok"] is False
        assert "画面过暗" in body["error"]
        assert manager.get("dark-job") is None
    finally:
        manager.stop()
