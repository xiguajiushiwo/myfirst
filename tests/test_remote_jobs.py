import io
import time
from types import SimpleNamespace

from PIL import Image

from app.remote_jobs import RemoteJobManager


def upload_image(color):
    content = io.BytesIO()
    Image.new("RGB", (800, 600), color).save(content, "JPEG")
    content.seek(0)
    return SimpleNamespace(file=content)


def wait_for(manager, job_id, status, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = manager.get(job_id)
        if state and state["status"] == status:
            return state
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {status}")


def test_remote_job_processes_images_and_is_idempotent(tmp_path):
    calls = []

    def processor(state, on_stage):
        calls.append(state["job_id"])
        on_stage("recognize", "OCR")
        return {"ok": True, "sticks": [{"slot_pos": 1, "sn": "SN-1"}]}

    manager = RemoteJobManager(tmp_path, processor=processor)
    manager.start()
    try:
        state, created = manager.submit(
            job_id="QC-01-test", front=upload_image("red"), back=upload_image("blue"),
            station_id="QC-01", operator="tester", batch_id=12,
            template_id="samsung-4up-0808", current_year=2026, threshold=0.72,
            metadata={"camera": "hik"},
        )
        assert created is True
        done = wait_for(manager, state["job_id"], "completed")
        assert done["result"]["sticks"][0]["sn"] == "SN-1"
        assert done["batch_id"] == 12
        assert done["template_id"] == "samsung-4up-0808"
        assert done["current_year"] == 2026
        assert done["threshold"] == 0.72
        assert done["images"]["front"]["width"] == 800

        existing, created = manager.submit(
            job_id="QC-01-test", front=upload_image("black"), back=upload_image("white"),
            station_id="QC-01",
        )
        assert created is False
        assert existing["status"] == "completed"
        assert calls == ["QC-01-test"]
    finally:
        manager.stop()


def test_remote_job_preserves_uploaded_jpeg_bytes(tmp_path):
    image = upload_image("red")
    original = image.file.getvalue()
    manager = RemoteJobManager(tmp_path, processor=lambda *_args: {"ok": True})
    manager.start()
    try:
        state, _ = manager.submit(
            job_id="preserve-jpeg", front=image, back=upload_image("blue"), station_id="QC-01",
        )
        wait_for(manager, state["job_id"], "completed")
        assert (tmp_path / "preserve-jpeg" / "front.jpg").read_bytes() == original
    finally:
        manager.stop()


def test_failed_remote_job_can_retry(tmp_path):
    attempts = []

    def processor(state, on_stage):
        attempts.append(state["job_id"])
        if len(attempts) == 1:
            raise RuntimeError("temporary failure")
        return {"ok": True}

    manager = RemoteJobManager(tmp_path, processor=processor)
    manager.start()
    try:
        state, _ = manager.submit(
            job_id="retry-job", front=upload_image("red"), back=upload_image("blue"),
            station_id="QC-02",
        )
        failed = wait_for(manager, state["job_id"], "failed")
        assert "temporary failure" in failed["error"]
        manager.retry(state["job_id"])
        done = wait_for(manager, state["job_id"], "completed")
        assert done["result"]["ok"] is True
        assert len(attempts) == 2
    finally:
        manager.stop()
