from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAMERA_HTML = ROOT / "web" / "camera.html"


def _script() -> str:
    return CAMERA_HTML.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    marker = f"function {name}"
    start = source.index(marker)
    next_function = source.find("\nfunction ", start + len(marker))
    return source[start:] if next_function == -1 else source[start:next_function]


def test_manual_capture_clears_previous_result_before_request():
    body = _function_body(_script(), "manualCapture")

    assert "clearInspectionDisplay();" in body
    assert body.index("clearInspectionDisplay();") < body.index("fetch(CLIENT_AGENT + \"/capture-and-recognize\"")


def test_record_polling_does_not_replay_old_results_during_new_capture():
    body = _function_body(_script(), "refreshFromRecords")

    assert "if(state.captureInProgress) return;" in body
    assert "if(state.blockHistoricalReplay) return;" in body
    assert "state.currentInspectionId" in body
