import time

from app import metrics, services
from app.recognition.date_parser import DateCode


def test_vl_usage_delta_is_per_inspection():
    before = {"calls": 10, "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    after = {"calls": 13, "prompt_tokens": 145, "completion_tokens": 29, "total_tokens": 174}
    assert metrics.vl_usage_delta(before, after) == {
        "calls": 3,
        "prompt_tokens": 45,
        "completion_tokens": 9,
        "total_tokens": 54,
    }


def test_rule_codes_are_assigned_to_physical_slots():
    code = DateCode("2517", "dram", 2025, 17, "2025-04-21", 0.9, "SEC 2517",
                    box=[[120, 20], [140, 20], [140, 40], [120, 40]])
    code._side = "front"
    core = {"all_codes": [code]}
    slot_info = {
        "occupied_slots": [0, 1],
        "boxes": {("front", 0): [0, 0, 100, 100], ("front", 1): [100, 0, 200, 100]},
        "occupancy": {"front": []},
    }
    services._assign_rule_codes_to_slots(core, slot_info)
    assert code.slot == 1
    assert core["stick_total"] == 2


def test_parallel_local_label_decode_uses_wall_clock(monkeypatch):
    def label(_path):
        time.sleep(0.05)
        return {"sn": "SN1", "src": "barcode"}

    monkeypatch.setattr(services, "_read_label", label)
    crops = {slot: (f"front-{slot}", f"back-{slot}") for slot in range(4)}
    started = time.perf_counter()
    labels, timing, _ = services._local_label_branch(crops)
    elapsed = time.perf_counter() - started

    assert len(labels) == 4
    assert elapsed < 0.2
    assert timing["local_decode_parallel"] < 0.2
    assert timing["label_decode"] >= 0.045


def test_record_keeps_complete_barcode_payload():
    label = {
        "sn": "ABC123", "model": "M321", "brand": "Samsung", "spec": "32GB PC5-5600",
        "mfg": "LOT9", "raw": "(L)32GB PC5-5600(S)ABC123(P)M321(M)LOT9", "src": "barcode",
    }
    rec = services.build_record({"dates": {}}, {}, label, "tester")
    assert rec["label_data"]["raw"] == label["raw"]
    assert rec["label_data"]["sn"] == "ABC123"


def test_record_verdict_depends_only_on_date():
    rec = services.build_record(
        {"dates": {"date_ok": True}},
        {"ok": True, "comp_ok": False, "appearance_fails": ["外观异常"]},
        {},
        "tester",
    )

    assert rec["verdict"] == "pass"
    assert rec["comp_ok"] is None
    assert "外观异常" not in rec["fail_desc"]
    assert rec["sn_unread"] is True
