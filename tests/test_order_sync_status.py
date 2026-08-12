import json

from app.routers import batches


def _reset_state():
    batches._sync_state.update(
        running=False,
        last_started=0.0,
        last_success=0.0,
        last_full_success=0.0,
        last_mode="",
        last_imported=0,
        last_error="",
        last_error_at=0.0,
        consecutive_failures=0,
    )


def test_sync_failure_is_persisted_and_reported(monkeypatch, tmp_path):
    state_file = tmp_path / "kingdee_sync_state.json"
    monkeypatch.setattr(batches, "_SYNC_STATE_FILE", state_file)
    monkeypatch.setattr(batches.kingdee, "fetch_orders", lambda **_kwargs: {
        "ok": False,
        "error": "token已过期",
    })
    _reset_state()

    result = batches._sync_kingdee(full=False, force=True)
    status = batches.sync_status()

    assert result["ok"] is False
    assert status["last_mode"] == "incremental"
    assert status["last_error"] == "token已过期"
    assert status["last_error_at"] > 0
    assert status["consecutive_failures"] == 1
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["last_error"] == "token已过期"


def test_success_clears_previous_sync_error(monkeypatch, tmp_path):
    monkeypatch.setattr(batches, "_SYNC_STATE_FILE", tmp_path / "kingdee_sync_state.json")
    monkeypatch.setattr(batches.kingdee, "fetch_orders", lambda **_kwargs: {
        "ok": True,
        "orders": [],
        "total": 0,
    })
    monkeypatch.setattr(batches, "_import_orders", lambda *_args, **_kwargs: {
        "ok": True,
        "imported": 0,
    })
    _reset_state()
    batches._sync_state.update(last_error="旧错误", last_error_at=1.0, consecutive_failures=2)

    result = batches._sync_kingdee(full=True, force=True)
    status = batches.sync_status()

    assert result["ok"] is True
    assert status["last_mode"] == "full"
    assert status["last_error"] == ""
    assert status["last_error_at"] == 0
    assert status["consecutive_failures"] == 0
