from types import SimpleNamespace

from app.recognition.visualize import EMPTY_COLOR, RAW_COLOR, _label_and_color


TYPE_COLOR = (1, 2, 3)


def _code(code_type, *, year=2025, week=34, raw="", idx=None, status="ok"):
    return SimpleNamespace(
        code_type=code_type,
        year=year,
        week=week,
        raw=raw,
        idx=idx,
        status=status,
    )


def test_dram_label_contains_only_the_date():
    label, color = _label_and_color(_code("dram", idx=3), TYPE_COLOR)

    assert label == "25年34周"
    assert color == TYPE_COLOR


def test_controller_label_uses_controller_prefix():
    label, color = _label_and_color(_code("controller"), TYPE_COLOR)

    assert label == "主：25年34周"
    assert color == TYPE_COLOR


def test_pcb_label_uses_pcb_prefix():
    label, color = _label_and_color(
        _code("pcb", year=2025, week=40), TYPE_COLOR
    )

    assert label == "PCB：25年40周"
    assert color == TYPE_COLOR


def test_covered_controller_label_says_covered():
    label, color = _label_and_color(
        _code("controller", week=0, raw="", status="covered"), TYPE_COLOR
    )

    assert label == "主：被遮挡"
    assert color == EMPTY_COLOR


def test_unparsed_labels_keep_type_but_do_not_invent_a_date():
    assert _label_and_color(
        _code("dram", week=0, raw="OCR123", idx=2), TYPE_COLOR
    ) == ("OCR123", RAW_COLOR)
    assert _label_and_color(
        _code("controller", week=0, raw=""), TYPE_COLOR
    ) == ("主：未识别", EMPTY_COLOR)
    assert _label_and_color(
        _code("pcb", week=0, raw="253X"), TYPE_COLOR
    ) == ("PCB：253X", RAW_COLOR)
