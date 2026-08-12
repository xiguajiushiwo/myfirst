from PIL import Image

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


def test_pcb_reader_accepts_strong_orientation_winner(monkeypatch):
    values = iter([
        (2022, 2, "2202", 0.72, "2202", 0.72, None),
        (2025, 34, "2534", 0.995, "TKK2534CFA1", 0.995, None),
    ])
    monkeypatch.setattr(geo_ocr, "_eval_crop", lambda *_args: next(values))

    year, week, raw, *_ = geo_ocr._read_controller_box(
        object(), Image.new("RGB", (40, 20), (30, 60, 40)), 2026)

    assert (year, week, raw) == (2025, 34, "2534")


def test_front_geo_recognizes_controller_and_marks_covered(monkeypatch, tmp_path):
    image_path = tmp_path / "front.png"
    Image.new("RGB", (200, 100), (30, 60, 40)).save(image_path)
    monkeypatch.setattr(geo_ocr, "_tpl_fixed", lambda *_args, **_kwargs: {
        1: {"controller": [0.0, 0.0, 0.4, 0.8]},
        2: {"controller": [0.5, 0.0, 0.9, 0.8]},
    })
    dram = geo_ocr._mk("dram", "552", 2025, 52, 0.99, "SEC552", None, "", "ok")
    dram.slot = 0
    monkeypatch.setattr(geo_ocr, "recognize_side", lambda *_args, **_kwargs: [dram])
    monkeypatch.setattr(geo_ocr, "get_component_engine", lambda: object())
    monkeypatch.setattr(geo_ocr, "_box_kind", lambda _img, box: "label" if box[0][0] else "chip")
    controller_reads = []
    pending4 = []

    def fake_controller_read(*_args):
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
    monkeypatch.setattr(geo_ocr, "recognize_side", lambda *_args, **_kwargs: [dram])
    monkeypatch.setattr(geo_ocr, "get_component_engine", lambda: object())
    monkeypatch.setattr(geo_ocr, "_read_pcb_box", lambda *_args: (
        2025, 43, "2543", 0.99, "E3 2543", None, "倒印180度"))
    monkeypatch.setattr(geo_ocr, "_vl_fallback_yyww", lambda *_args: None)

    result = geo_ocr.recognize_geo(str(image_path), 2026, side="back")

    assert [(item.code_type, item.raw) for item in result] == [
        ("dram", "552"),
        ("pcb", "2543"),
    ]


def test_low_confidence_date_stays_for_manual_review():
    code = geo_ocr._mk("dram", "55?", None, None, 0.2, "SEC55?", None, "", "unknown")

    region_ocr._vl_fallback_dram([(code, Image.new("RGB", (20, 10)))], 2026)

    assert not code.week
    assert code.model_confidence is None
    assert "人工复核" in code.note
