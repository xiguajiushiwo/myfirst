import time

from PIL import Image

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


def test_pre_crops_maps_back_visual_slots_to_physical_slots(monkeypatch, tmp_path):
    front = tmp_path / "front.png"
    back = tmp_path / "back.png"
    Image.new("RGB", (40, 20), (255, 255, 255)).save(front)
    Image.new("RGB", (40, 20), (255, 255, 255)).save(back)
    tpl = {
        "sides": {
            "front": {"slots": [[0, 0, 0.25, 1], [0.25, 0, 0.5, 1],
                                [0.5, 0, 0.75, 1], [0.75, 0, 1, 1]],
                      "slot_axis": "horizontal"},
            "back": {"slots": [[0, 0, 0.25, 1], [0.25, 0, 0.5, 1],
                               [0.5, 0, 0.75, 1], [0.75, 0, 1, 1]],
                     "slot_axis": "horizontal",
                     "physical_slot_order": [3, 2, 1, 0]},
        }
    }
    monkeypatch.setattr(services, "_slot_template", lambda _template_id: ("test", tpl))
    monkeypatch.setattr(services, "_crop_slot", lambda _src, box, tag: f"{tag}:{box}")
    monkeypatch.setattr(services, "UPLOAD_DIR", str(tmp_path))

    info = services._pre_crops({"front": str(front), "back": str(back)}, "u1", "geo", "test")

    assert info["boxes"][("back", 0)] == [30, 0, 40, 20]
    assert info["boxes"][("back", 3)] == [0, 0, 10, 20]


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


def test_record_keeps_date_status_for_frontend():
    rec = services.build_record(
        {"dates": {"controller_status": "covered", "pcb_status": "raw"}},
        {},
        {"sn": "SN1"},
        "tester",
    )

    assert rec["label_data"]["date_status"] == {
        "controller": "covered",
        "pcb": "raw",
    }


def test_stick_summary_exposes_chip_date_details():
    record = {
        "slot_pos": 1,
        "verdict": "pass",
        "sn": "SN-1",
        "brand": "Samsung",
        "model": "M321R",
        "capacity": "64GB",
        "frequency": "5600",
        "spec": "64GB 2Rx4 PC5-5600",
        "mfg": "LOT-9",
        "label_data": {"date_status": {"controller": "covered"}},
        "sn_unread": False,
        "controller_date": None,
        "pcb_date": "202540",
        "storage_chips": [
            {"idx": 1, "side": "front", "yyyyww": "202534", "status": "ok"},
            {"idx": 2, "side": "back", "yyyyww": "202535", "status": "ok"},
        ],
        "fail_desc": "",
        "date_ok": True,
        "comp_ok": None,
        "gold_finger_ok": None,
        "chip_mark_ok": None,
    }

    summary = services._stick_summary(record, 9)

    assert summary["capacity"] == "64GB"
    assert summary["pcb_date"] == "202540"
    assert summary["controller_status"] == "covered"
    assert summary["storage_count"] == 2
    assert summary["storage_chips"][0]["yyyyww"] == "202534"
