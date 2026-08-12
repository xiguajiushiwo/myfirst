"""Compare local date OCR on color and grayscale front/back test photos."""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.recognition import template_store
from app.recognition.region_ocr import recognize_side


OUTPUT = ROOT / "outputs" / "grayscale_benchmark.json"


def summarize(path: Path, side: str, template_id: str) -> dict:
    started = time.perf_counter()
    codes = recognize_side(
        str(path), side, current_year=2026, template_id=template_id,
        code_types={"dram"},
    )
    confidences = [float(code.ocr_confidence or 0) for code in codes]
    return {
        "side": side,
        "image": str(path.relative_to(ROOT)),
        "total": len(codes),
        "valid_dates": sum(bool(code.year and code.week) for code in codes),
        "ocr_nonempty": sum(bool(code.ocr_raw) for code in codes),
        "avg_ocr_confidence": round(sum(confidences) / len(confidences), 4) if confidences else 0,
        "dates": [
            f"{code.year:04d}{code.week:02d}" if code.year and code.week else None
            for code in codes
        ],
        "ocr_raw": [code.ocr_raw for code in codes],
        "elapsed_sec": round(time.perf_counter() - started, 2),
    }


def main() -> None:
    os.environ.pop("DASHSCOPE_API_KEY", None)
    template_id = template_store.default_template_id()
    cases = {
        "color": {"front": ROOT / "test_photos" / "f.png", "back": ROOT / "test_photos" / "b.png"},
        "grayscale": {
            "front": ROOT / "test_photos" / "grayscale" / "f.png",
            "back": ROOT / "test_photos" / "grayscale" / "b.png",
        },
    }
    result = {"template_id": template_id, "cloud_fallback": False, "cases": {}}
    for name, sides in cases.items():
        started = time.perf_counter()
        result["cases"][name] = {
            "sides": [summarize(path, side, template_id) for side, path in sides.items()],
            "elapsed_sec": round(time.perf_counter() - started, 2),
        }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
