from client.qc_client import QCClient


def test_wait_result_returns_completed_payload(monkeypatch, tmp_path):
    client = QCClient({
        "server_url": "http://server:8001",
        "api_key": "key",
        "station_id": "QC-01",
        "cache_dir": str(tmp_path),
        "timeout_seconds": 2,
    })
    states = iter([
        {"ok": True, "job": {"status": "processing"}},
        {"ok": True, "job": {"status": "completed", "result": {"ok": True, "sticks": []}}},
    ])
    monkeypatch.setattr(client, "get_job", lambda _: next(states))
    monkeypatch.setattr("client.qc_client.time.sleep", lambda _: None)

    assert client.wait_result("job-1") == {"ok": True, "sticks": []}


def test_wait_result_raises_server_error(monkeypatch, tmp_path):
    client = QCClient({
        "server_url": "http://server:8001",
        "api_key": "key",
        "station_id": "QC-01",
        "cache_dir": str(tmp_path),
        "timeout_seconds": 2,
    })
    monkeypatch.setattr(client, "get_job", lambda _: {
        "ok": True, "job": {"status": "failed", "error": "OCR failed"},
    })

    try:
        client.wait_result("job-2")
    except RuntimeError as exc:
        assert str(exc) == "OCR failed"
    else:
        raise AssertionError("expected RuntimeError")
