from PIL import Image, ImageDraw

from app.recognition import geo_ocr
from app.recognition import region_ocr


def test_pcb_reader_retries_with_grayscale(monkeypatch):
    calls = []

    def fake_eval(_engine, image, _year, _reader, _roi):
        is_gray = image.getpixel((0, 0))[0] == image.getpixel((0, 0))[1]
        calls.append(is_gray)
        if is_gray and len(calls) == 3:
            return 2025, 43, "2543", 0.95, "E3 2543", 0.95, None
        return None, None, "", 0.0, "", 0.0, None

    monkeypatch.setattr(geo_ocr, "_eval_crop", fake_eval)
    crop = Image.new("RGB", (40, 20), (20, 90, 140))

    year, week, raw, *_ = geo_ocr._read_pcb_box(object(), crop, 2026)

    assert (year, week, raw) == (2025, 43, "2543")
    assert calls == [False, False, True, True]


def test_pcb_reader_fast_path_stops_after_first_confident_read(monkeypatch):
    calls = []

    def fake_eval(*_args):
        calls.append(True)
        return 2025, 43, "2543", 0.91, "E3 2543", 0.91, None

    monkeypatch.setattr(geo_ocr, "_eval_crop", fake_eval)

    year, week, raw, score, *_ = geo_ocr._read_pcb_box(
        object(), Image.new("RGB", (40, 20), (20, 90, 140)), 2026, fast=True)

    assert (year, week, raw, score) == (2025, 43, "2543", 0.91)
    assert len(calls) == 1


def test_pcb_reader_fast_preferred_rotation_stops_after_first_read(monkeypatch):
    calls = []

    def fake_eval(*_args):
        calls.append(True)
        return 2025, 34, "2534", 0.99, "TKK2534CFA1", 0.99, None

    monkeypatch.setattr(geo_ocr, "_eval_crop", fake_eval)

    year, week, raw, score, *_rest, note = geo_ocr._read_pcb_box(
        object(), Image.new("RGB", (40, 20), (20, 90, 140)), 2026,
        fast=True, preferred_rotation=180)

    assert (year, week, raw, score) == (2025, 34, "2534", 0.99)
    assert note == "倒印180°"
    assert len(calls) == 1


def test_pcb_reader_fast_preferred_color_channel_before_alternate_rotation(monkeypatch):
    calls = []
    values = iter([
        (None, None, "", 0.0, "", 0.0, None),
        (2025, 46, "2546", 0.93, "2546", 0.93, None),
    ])

    def fake_eval(*_args):
        calls.append(True)
        return next(values)

    monkeypatch.setattr(geo_ocr, "_eval_crop", fake_eval)

    year, week, raw, score, *_rest, note = geo_ocr._read_pcb_box(
        object(), Image.new("RGB", (100, 60), (30, 80, 40)), 2026,
        fast=True, preferred_rotation=0)

    assert (year, week, raw, score) == (2025, 46, "2546", 0.93)
    assert note == "颜色差分局部增强"
    assert len(calls) == 2


def test_pcb_reader_uses_color_difference_fallback(monkeypatch):
    values = iter([
        (None, None, "", 0.0, "", 0.0, None),
        (None, None, "", 0.0, "", 0.0, None),
        (None, None, "", 0.0, "", 0.0, None),
        (None, None, "", 0.0, "", 0.0, None),
        (2025, 40, "2540", 0.993, "D4 2540", 0.993, None),
        (2025, 40, "2540", 0.821, "D4 2540", 0.821, None),
    ])
    monkeypatch.setattr(geo_ocr, "_eval_crop", lambda *_args: next(values))

    year, week, raw, score, *_rest, note = geo_ocr._read_pcb_box(
        object(), Image.new("RGB", (100, 60), (30, 80, 40)), 2026)

    assert (year, week, raw, score) == (2025, 40, "2540", 0.993)
    assert "颜色差分" in note


def test_pcb_reader_accepts_strong_orientation_winner(monkeypatch):
    values = iter([
        (2022, 2, "2202", 0.72, "2202", 0.72, None),
        (2025, 34, "2534", 0.995, "TKK2534CFA1", 0.995, None),
    ])
    monkeypatch.setattr(geo_ocr, "_eval_crop", lambda *_args: next(values))

    year, week, raw, *_ = geo_ocr._read_controller_box(
        object(), Image.new("RGB", (40, 20), (30, 60, 40)), 2026)

    assert (year, week, raw) == (2025, 34, "2534")


def test_pcb_reader_accepts_same_date_from_both_orientations(monkeypatch):
    values = iter([
        (2025, 43, "2543", 0.946, "E3 2543", 0.946, None),
        (2025, 43, "2543", 0.806, "3 2543", 0.806, None),
    ])
    monkeypatch.setattr(geo_ocr, "_eval_crop", lambda *_args: next(values))

    year, week, raw, score, *_rest, note = geo_ocr._read_pcb_box(
        object(), Image.new("RGB", (40, 20), (30, 60, 40)), 2026)

    assert (year, week, raw, score) == (2025, 43, "2543", 0.946)
    assert "一致" in note


def test_pcb_reader_maps_rotated_scaled_hit_back_to_crop(monkeypatch):
    hit = ("E3 2543", [[20, 4], [60, 4], [60, 16], [20, 16]])
    values = iter([
        (None, None, "", 0.0, "", 0.0, None),
        (2025, 43, "2543", 0.99, "E3 2543", 0.99, hit),
    ])
    monkeypatch.setattr(geo_ocr, "_eval_crop", lambda *_args: next(values))

    *_, restored, note = geo_ocr._read_pcb_box(
        object(), Image.new("RGB", (40, 20), (30, 60, 40)), 2026)

    assert note == "倒印180°"
    assert restored == (
        "E3 2543",
        [[30.0, 18.0], [10.0, 18.0], [10.0, 12.0], [30.0, 12.0]],
    )


def test_front_geo_recognizes_controller_and_marks_covered(monkeypatch, tmp_path):
    image_path = tmp_path / "front.png"
    Image.new("RGB", (200, 100), (30, 60, 40)).save(image_path)
    monkeypatch.setattr(geo_ocr, "_tpl_fixed", lambda *_args, **_kwargs: {
        1: {"controller": [0.0, 0.0, 0.4, 0.8]},
        2: {"controller": [0.5, 0.0, 0.9, 0.8]},
    })
    dram = geo_ocr._mk("dram", "552", 2025, 52, 0.99, "SEC552", None, "", "ok")
    dram.slot = 0
    monkeypatch.setattr(geo_ocr, "recognize_dynamic_drams", lambda *_args, **_kwargs: [dram])
    monkeypatch.setattr(geo_ocr, "get_component_engine", lambda: object())
    monkeypatch.setattr(geo_ocr, "_box_kind", lambda _img, box: "label" if box[0][0] else "chip")
    controller_reads = []
    pending4 = []

    def fake_controller_read(*_args, **_kwargs):
        controller_reads.append(True)
        return 2025, 34, "2534", 0.99, "TKK2534CFA1", None, "倒印180度"

    monkeypatch.setattr(geo_ocr, "_read_controller_box", fake_controller_read)
    monkeypatch.setattr(geo_ocr, "_vl_fallback_yyww", lambda rows, _year: pending4.extend(rows))

    result = geo_ocr.recognize_geo(str(image_path), 2026, side="front")

    assert [(item.code_type, item.status, item.raw) for item in result] == [
        ("dram", "ok", "552"),
        ("controller", "ok", "2534"),
        ("controller", "covered", ""),
    ]
    assert len(controller_reads) == 1
    assert pending4 == []


def test_back_geo_only_adds_pcb_not_pmic_or_sot(monkeypatch, tmp_path):
    image_path = tmp_path / "back.png"
    Image.new("RGB", (100, 100), (30, 60, 40)).save(image_path)
    monkeypatch.setattr(geo_ocr, "_tpl_fixed", lambda *_args, **_kwargs: {
        1: {
            "pcb": [0.0, 0.0, 0.8, 0.3],
            "pmic": [0.0, 0.3, 0.8, 0.6],
            "sot": [0.0, 0.6, 0.8, 0.9],
        },
    })
    dram = geo_ocr._mk("dram", "552", 2025, 52, 0.99, "SEC552", None, "", "ok")
    dram.slot = 0
    monkeypatch.setattr(geo_ocr, "recognize_dynamic_drams", lambda *_args, **_kwargs: [dram])
    monkeypatch.setattr(geo_ocr, "get_component_engine", lambda: object())
    tight_hit = ("E3 2543", [[20, 4], [30, 4], [30, 8], [20, 8]])
    monkeypatch.setattr(geo_ocr, "_read_pcb_box", lambda *_args, **_kwargs: (
        2025, 43, "2543", 0.99, "E3 2543", tight_hit, "倒印180度"))
    monkeypatch.setattr(geo_ocr, "_vl_fallback_yyww", lambda *_args: None)

    result = geo_ocr.recognize_geo(str(image_path), 2026, side="back")

    assert [(item.code_type, item.raw) for item in result] == [
        ("dram", "552"),
        ("pcb", "2543"),
    ]
    assert result[1].box == [[0, 0], [80, 0], [80, 30], [0, 30]]


def test_dynamic_back_drams_rotate_back_and_assign_runtime_slot(monkeypatch, tmp_path):
    image_path = tmp_path / "back.png"
    Image.new("RGB", (100, 80), (20, 40, 30)).save(image_path)
    monkeypatch.setattr(geo_ocr, "_tpl_layout", lambda *_args: {
        "slots": [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 1.0, 1.0]],
        "slot_axis": "horizontal",
    })
    detected = geo_ocr._mk(
        "dram", "552", 2025, 52, 0.99, "SEC552",
        [[10, 10], [20, 10], [20, 20], [10, 20]], "", "ok")
    monkeypatch.setattr(geo_ocr, "recognize_rules", lambda *_args, **_kwargs: [detected])
    monkeypatch.setattr(geo_ocr, "_complete_back_grid", lambda rows, *_args: rows)

    result = geo_ocr.recognize_dynamic_drams(
        str(image_path), "back", 2026, "test-template")

    assert result[0].box == [[90, 70], [80, 70], [80, 60], [90, 60]]
    assert result[0].slot == 1


def test_dynamic_back_drams_apply_physical_slot_order(monkeypatch, tmp_path):
    image_path = tmp_path / "back.png"
    Image.new("RGB", (100, 80), (20, 40, 30)).save(image_path)
    monkeypatch.setattr(geo_ocr, "_tpl_layout", lambda *_args: {
        "slots": [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 1.0, 1.0]],
        "slot_axis": "horizontal",
        "physical_slot_order": [1, 0],
    })
    detected = geo_ocr._mk(
        "dram", "552", 2025, 52, 0.99, "SEC552",
        [[10, 10], [20, 10], [20, 20], [10, 20]], "", "ok")
    monkeypatch.setattr(geo_ocr, "recognize_rules", lambda *_args, **_kwargs: [detected])
    monkeypatch.setattr(geo_ocr, "_complete_back_grid", lambda rows, *_args: rows)

    result = geo_ocr.recognize_dynamic_drams(
        str(image_path), "back", 2026, "test-template")

    assert result[0].box == [[90, 70], [80, 70], [80, 60], [90, 60]]
    assert result[0].slot == 0


def test_dynamic_drams_use_template_tile_bands(monkeypatch, tmp_path):
    image_path = tmp_path / "front.png"
    Image.new("RGB", (100, 80), (20, 40, 30)).save(image_path)
    monkeypatch.setattr(geo_ocr, "_tpl_layout", lambda *_args: {
        "slots": [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 1.0, 1.0]],
        "slot_axis": "horizontal",
        "dram_tile_bands": 1,
    })
    seen = {}

    def fake_rules(*_args, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(geo_ocr, "recognize_rules", fake_rules)

    geo_ocr.recognize_dynamic_drams(str(image_path), "front", 2026, "test-template")

    assert seen["tile_bands"] == 1


def test_back_grid_adds_unknown_without_fabricating_date():
    slot_rects = [[slot / 4, 0.0, (slot + 1) / 4, 1.0] for slot in range(4)]
    rows = []
    for slot, rect in enumerate(slot_rects):
        for col in range(2):
            for row in range(10):
                if slot == 3 and col == 1 and row == 9:
                    continue
                center_x = (rect[0] + (0.3 + col * 0.4) * (rect[2] - rect[0])) * 400
                center_y = (0.05 + row * 0.1) * 1000
                code = geo_ocr._mk(
                    "dram", "552", 2025, 52, 0.99, "SEC552",
                    [[center_x - 3, center_y - 4], [center_x + 3, center_y - 4],
                     [center_x + 3, center_y + 4], [center_x - 3, center_y + 4]],
                    "", "ok")
                code.slot = slot
                rows.append(code)

    result = geo_ocr._complete_back_grid(rows, slot_rects, 400, 1000)
    missing = [code for code in result if code.status == "unknown"]

    assert len(result) == 80
    assert len(missing) == 1
    assert missing[0].slot == 3
    assert missing[0].raw == ""
    assert missing[0].year == 0
    assert missing[0].week == 0


def test_back_grid_rereads_completed_missing_cell(monkeypatch):
    slot_rects = [[slot / 4, 0.0, (slot + 1) / 4, 1.0] for slot in range(4)]
    rows = []
    for slot, rect in enumerate(slot_rects):
        for col in range(2):
            for row in range(10):
                if slot == 2 and col == 1 and row == 4:
                    continue
                center_x = (rect[0] + (0.3 + col * 0.4) * (rect[2] - rect[0])) * 400
                center_y = (0.05 + row * 0.1) * 1000
                code = geo_ocr._mk(
                    "dram", "552", 2025, 52, 0.99, "SEC552",
                    [[center_x - 3, center_y - 4], [center_x + 3, center_y - 4],
                     [center_x + 3, center_y + 4], [center_x - 3, center_y + 4]],
                    "", "ok")
                code.slot = slot
                rows.append(code)
    monkeypatch.setattr(geo_ocr, "get_component_engine", lambda: object())
    monkeypatch.setattr(geo_ocr, "_read_dram_box", lambda *_args: (
        2025, 1, "501", 0.97, "SEC501",
        ("501", [[1, 2], [20, 2], [20, 12], [1, 12]]), ""))

    result = geo_ocr._complete_back_grid(
        rows, slot_rects, 400, 1000, Image.new("RGB", (400, 1000)), 2026)
    filled = [code for code in result if code.slot == 2 and code.raw == "501"]

    assert len(result) == 80
    assert len(filled) == 1
    assert filled[0].status == "ok"
    assert filled[0].year == 2025
    assert filled[0].week == 1
    assert not [code for code in result if code.status == "unknown"]


def test_dynamic_slot_assignment_groups_two_chip_lanes_per_stick():
    slot_rects = [[slot / 4, 0.0, (slot + 1) / 4, 1.0] for slot in range(4)]
    rows = []
    for slot in range(4):
        for lane in range(2):
            center_x = (slot / 4 + 0.08 + lane * 0.08) * 1000
            for row in range(3):
                code = geo_ocr._mk(
                    "dram", "552", 2025, 52, 0.99, "SEC552",
                    [[center_x - 3, row * 10], [center_x + 3, row * 10],
                     [center_x + 3, row * 10 + 4], [center_x - 3, row * 10 + 4]],
                    "", "ok")
                rows.append(code)

    geo_ocr._assign_dynamic_slots(rows, slot_rects, 1000, 100, "horizontal")

    assert [sum(code.slot == slot for code in rows) for slot in range(4)] == [6, 6, 6, 6]


def test_dynamic_slot_assignment_uses_template_slot_bounds_for_sparse_lanes():
    slot_rects = [
        [0.0, 0.0, 0.2757, 1.0],
        [0.2757, 0.0, 0.4881, 1.0],
        [0.4881, 0.0, 0.6998, 1.0],
        [0.6998, 0.0, 1.0, 1.0],
    ]
    centers = [0.256, 0.402, 0.463, 0.61, 0.672, 0.82, 0.88, 0.95]
    rows = []
    for center in centers:
        x = center * 1000
        code = geo_ocr._mk(
            "dram", "552", 2025, 52, 0.99, "SEC552",
            [[x - 3, 10], [x + 3, 10], [x + 3, 20], [x - 3, 20]],
            "", "ok")
        rows.append(code)

    geo_ocr._assign_dynamic_slots(rows, slot_rects, 1000, 100, "horizontal")

    assert [code.slot for code in rows] == [0, 1, 1, 2, 2, 3, 3, 3]


def test_dynamic_drams_ignore_component_box_false_dates(monkeypatch, tmp_path):
    image_path = tmp_path / "front.png"
    Image.new("RGB", (1000, 600), (20, 40, 30)).save(image_path)
    monkeypatch.setattr(geo_ocr, "_tpl_layout", lambda *_args: {
        "slots": [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 1.0, 1.0]],
        "slot_axis": "horizontal",
        "boxes": [{
            "type": "controller",
            "box": [[0.20, 0.45], [0.30, 0.45], [0.30, 0.55], [0.20, 0.55]],
            "manual": True,
            "slot": 0,
        }],
    })
    false_component = geo_ocr._mk(
        "dram", "534", 2025, 34, 0.99, "RCD2534",
        [[240, 290], [260, 290], [260, 310], [240, 310]], "", "ok")
    real_dram = geo_ocr._mk(
        "dram", "552", 2025, 52, 0.99, "SEC552",
        [[120, 100], [140, 100], [140, 120], [120, 120]], "", "ok")
    monkeypatch.setattr(geo_ocr, "recognize_rules", lambda *_args, **_kwargs: [false_component, real_dram])

    result = geo_ocr.recognize_dynamic_drams(str(image_path), "front", 2026, "test-template")

    assert [code.raw for code in result] == ["552"]


def test_dynamic_drams_keep_real_chip_below_controller(monkeypatch, tmp_path):
    image_path = tmp_path / "front.png"
    Image.new("RGB", (1000, 600), (20, 40, 30)).save(image_path)
    monkeypatch.setattr(geo_ocr, "_tpl_layout", lambda *_args: {
        "slots": [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 1.0, 1.0]],
        "slot_axis": "horizontal",
        "boxes": [{
            "type": "controller",
            "box": [[0.20, 0.45], [0.30, 0.45], [0.30, 0.55], [0.20, 0.55]],
            "manual": True,
            "slot": 0,
        }],
    })
    false_component = geo_ocr._mk(
        "dram", "534", 2025, 34, 0.99, "RCD2534",
        [[240, 290], [260, 290], [260, 310], [240, 310]], "", "ok")
    real_dram_below = geo_ocr._mk(
        "dram", "501", 2025, 1, 0.99, "SEC501",
        [[240, 380], [260, 380], [260, 400], [240, 400]], "", "ok")
    monkeypatch.setattr(
        geo_ocr, "recognize_rules",
        lambda *_args, **_kwargs: [false_component, real_dram_below])

    result = geo_ocr.recognize_dynamic_drams(
        str(image_path), "front", 2026, "test-template")

    assert [code.raw for code in result] == ["501"]


def test_dynamic_drams_ignore_white_label_false_dates(monkeypatch, tmp_path):
    image_path = tmp_path / "front.png"
    image = Image.new("RGB", (1000, 600), (20, 40, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle([560, 100, 670, 520], fill=(245, 245, 245))
    image.save(image_path)
    monkeypatch.setattr(geo_ocr, "_tpl_layout", lambda *_args: {
        "slots": [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 1.0, 1.0]],
        "slot_axis": "horizontal",
    })
    false_label_date = geo_ocr._mk(
        "dram", "120", 2021, 20, 0.99, "W00E120 SAMSUNG",
        [[610, 220], [630, 220], [630, 240], [610, 240]], "", "ok")
    real_dram = geo_ocr._mk(
        "dram", "501", 2025, 1, 0.99, "SEC501",
        [[760, 220], [780, 220], [780, 240], [760, 240]], "", "ok")
    monkeypatch.setattr(
        geo_ocr, "recognize_rules",
        lambda *_args, **_kwargs: [false_label_date, real_dram])

    result = geo_ocr.recognize_dynamic_drams(
        str(image_path), "front", 2026, "test-template")

    assert [code.raw for code in result] == ["501"]


def test_back_dynamic_drams_ignore_pcb_box_after_rotation(monkeypatch, tmp_path):
    image_path = tmp_path / "back.png"
    Image.new("RGB", (1000, 600), (20, 40, 30)).save(image_path)
    monkeypatch.setattr(geo_ocr, "_tpl_layout", lambda *_args: {
        "slots": [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 1.0, 1.0]],
        "slot_axis": "horizontal",
        "boxes": [{
            "type": "pcb",
            "box": [[0.20, 0.45], [0.30, 0.45], [0.30, 0.55], [0.20, 0.55]],
            "manual": True,
            "slot": 0,
        }],
    })
    false_pcb_in_rotated_image = geo_ocr._mk(
        "dram", "546", 2025, 46, 0.99, "PCB2546",
        [[740, 290], [760, 290], [760, 310], [740, 310]], "", "ok")
    real_dram_in_rotated_image = geo_ocr._mk(
        "dram", "552", 2025, 52, 0.99, "SEC552",
        [[860, 480], [880, 480], [880, 500], [860, 500]], "", "ok")
    monkeypatch.setattr(
        geo_ocr, "recognize_rules",
        lambda *_args, **_kwargs: [false_pcb_in_rotated_image, real_dram_in_rotated_image])
    monkeypatch.setattr(geo_ocr, "_complete_back_grid", lambda rows, *_args: rows)

    result = geo_ocr.recognize_dynamic_drams(str(image_path), "back", 2026, "test-template")

    assert [code.raw for code in result] == ["552"]


def test_low_confidence_date_stays_for_manual_review():
    code = geo_ocr._mk("dram", "55?", None, None, 0.2, "SEC55?", None, "", "unknown")

    region_ocr._vl_fallback_dram([(code, Image.new("RGB", (20, 10)))], 2026)

    assert not code.week
    assert code.model_confidence is None
    assert "人工复核" in code.note
