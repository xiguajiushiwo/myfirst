from app.routers import cameras


def test_capture_and_analyze_adds_full_trace(monkeypatch, tmp_path):
    saved_trace = {}

    monkeypatch.setattr(cameras, "_next_seq_dir", lambda: (str(tmp_path), "0001"))

    def fake_capture(front_path, back_path):
        open(front_path, "wb").write(b"front")
        open(back_path, "wb").write(b"back")

    monkeypatch.setattr(cameras.hik, "capture_both", fake_capture)
    monkeypatch.setattr(cameras.services, "analyze_and_save", lambda *args, **kwargs: {
        "ok": True,
        "record_id": 42,
        "elapsed_sec": 1.5,
        "timing": {"recognize": 1.0, "inspect_label": 0.8, "save": 0.1, "total": 1.5},
        "token_usage": {"calls": 2, "prompt_tokens": 100, "completion_tokens": 20,
                        "total_tokens": 120, "details": []},
    })
    monkeypatch.setattr(cameras.db, "update_record_trace",
                        lambda record_ids, trace: saved_trace.update(ids=record_ids, trace=trace))

    result = cameras._capture_and_analyze(trigger_mode="manual")

    assert result["run_id"]
    assert result["trigger_mode"] == "manual"
    assert result["started_at"] and result["finished_at"]
    assert result["timing"]["capture"] >= 0
    assert result["timing"]["total"] >= result["timing"]["capture"]
    assert result["elapsed_sec"] == result["timing"]["total"]
    assert result["token_usage"]["total_tokens"] == 120
    assert saved_trace["ids"] == [42]
    assert saved_trace["trace"]["run_id"] == result["run_id"]


def test_stick_summary_exposes_label_and_order_fields():
    record = {
        "slot_pos": 2, "verdict": "pass", "sn": "SN-2", "brand": "Samsung",
        "model": "M321R", "capacity": "64GB", "frequency": "5600",
        "spec": "64GB 2Rx4 PC5-5600", "mfg": "LOT-9", "sn_unread": False,
        "controller_date": "202520", "pcb_date": "202519", "storage_chips": [],
        "fail_desc": "", "date_ok": True, "comp_ok": True,
        "gold_finger_ok": True, "chip_mark_ok": True,
    }

    summary = cameras.services._stick_summary(record, 9)

    assert summary["sn"] == "SN-2"
    assert summary["model"] == "M321R"
    assert summary["capacity"] == "64GB"
    assert summary["frequency"] == "5600"
    assert summary["spec"] == "64GB 2Rx4 PC5-5600"
    assert summary["mfg"] == "LOT-9"
