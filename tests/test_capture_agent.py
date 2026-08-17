import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from client import capture_agent
from client.qc_client import QCClient


def test_capture_agent_root_explains_local_service(monkeypatch, tmp_path):
    config = {
        "server_url": "http://server:8000",
        "api_key": "key",
        "station_id": "QC-01",
        "cache_dir": str(tmp_path / "cache"),
    }
    config_path = tmp_path / "client_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(capture_agent.cam_supervisor, "start", lambda: None)
    monkeypatch.setattr(capture_agent.cam_supervisor, "stop", lambda: None)
    monkeypatch.setattr(capture_agent.hik, "status", lambda: {"sdk": True, "devices": []})
    monkeypatch.setattr(capture_agent.QCClient, "health", lambda _self: {"ok": True})

    with TestClient(capture_agent.create_app(str(config_path))) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "云小圈客户机采集代理" in response.text
    assert "http://server:8000/camera" in response.text


def test_capture_agent_health_reports_server_connection(monkeypatch, tmp_path):
    config = {
        "server_url": "http://server:8000",
        "api_key": "key",
        "station_id": "QC-01",
        "cache_dir": str(tmp_path / "cache"),
    }
    config_path = tmp_path / "client_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(capture_agent.cam_supervisor, "start", lambda: None)
    monkeypatch.setattr(capture_agent.cam_supervisor, "stop", lambda: None)
    monkeypatch.setattr(capture_agent.hik, "status", lambda: {"sdk": True, "devices": []})
    monkeypatch.setattr(capture_agent.QCClient, "health", lambda _self: {"ok": True})

    with TestClient(capture_agent.create_app(str(config_path))) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["server"] == {"ok": True}


def test_capture_agent_preview_forwards_quality_and_fps(monkeypatch, tmp_path):
    config = {
        "server_url": "http://server:8000",
        "api_key": "key",
        "station_id": "QC-01",
        "cache_dir": str(tmp_path / "cache"),
    }
    config_path = tmp_path / "client_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    calls = []

    def fake_mjpeg(side, **kwargs):
        calls.append((side, kwargs))
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\nfake\r\n"

    monkeypatch.setattr(capture_agent.cam_supervisor, "start", lambda: None)
    monkeypatch.setattr(capture_agent.cam_supervisor, "stop", lambda: None)
    monkeypatch.setattr(capture_agent.hik, "status", lambda: {"sdk": True, "devices": []})
    monkeypatch.setattr(capture_agent.hik, "mjpeg", fake_mjpeg)

    with TestClient(capture_agent.create_app(str(config_path))) as client:
        response = client.get("/camera/preview?side=back&quality=82&max_fps=14")

    assert response.status_code == 200
    assert calls == [("back", {"quality": 82, "max_fps": 14})]


def test_capture_quality_guard_rejects_dark_images(tmp_path):
    client = QCClient({
        "server_url": "http://server:8000",
        "api_key": "key",
        "station_id": "QC-01",
        "cache_dir": str(tmp_path / "cache"),
    })
    front = tmp_path / "front.jpg"
    back = tmp_path / "back.jpg"
    cv2.imwrite(str(front), np.zeros((120, 160), dtype=np.uint8))
    cv2.imwrite(str(back), np.full((120, 160), 2, dtype=np.uint8))

    with pytest.raises(RuntimeError, match="画面过暗"):
        client._validate_capture_images(front, back)


def test_capture_quality_guard_accepts_visible_images(tmp_path):
    client = QCClient({
        "server_url": "http://server:8000",
        "api_key": "key",
        "station_id": "QC-01",
        "cache_dir": str(tmp_path / "cache"),
    })
    front = tmp_path / "front.jpg"
    back = tmp_path / "back.jpg"
    visible = np.full((120, 160), 65, dtype=np.uint8)
    visible[:, 50:110] = 120
    cv2.imwrite(str(front), visible)
    cv2.imwrite(str(back), visible)

    client._validate_capture_images(front, back)
