from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "app" / "recognition" / "templates" / "samsung-4up-0808.json"
WIDTH, HEIGHT = 4024, 3036

# The user-marked boxes in all_mb. Coordinates use [x0, y0, x1, y1].
FRONT_CONTROLLER = [548, 1386, 848, 1582]
BACK_PCB_MARKS = [
    [463, 1376, 787, 1494],
    [1305, 1368, 1625, 1449],
    [2164, 1383, 2455, 1477],
    [2890, 1363, 3289, 1439],
]

# Gold-finger centers measured in the current front frame. They are stable
# per-stick anchors and preserve small spacing differences between tray slots.
FRONT_ANCHORS = [467.5, 1290.0, 2126.0, 2959.0]


def polygon(rect: list[float]) -> list[list[float]]:
    x0, y0, x1, y1 = rect
    return [
        [round(x0 / WIDTH, 4), round(y0 / HEIGHT, 4)],
        [round(x1 / WIDTH, 4), round(y0 / HEIGHT, 4)],
        [round(x1 / WIDTH, 4), round(y1 / HEIGHT, 4)],
        [round(x0 / WIDTH, 4), round(y1 / HEIGHT, 4)],
    ]


def rotate_180(rect: list[float]) -> list[float]:
    x0, y0, x1, y1 = rect
    return [WIDTH - x1, HEIGHT - y1, WIDTH - x0, HEIGHT - y0]


def replace_fixed_boxes(template: dict) -> None:
    front = template["sides"]["front"]
    back = template["sides"]["back"]

    front["slot_axis"] = "horizontal"
    back["slot_axis"] = "horizontal"

    controller_width = FRONT_CONTROLLER[2] - FRONT_CONTROLLER[0]
    controller_boxes = []
    for slot, anchor in enumerate(FRONT_ANCHORS):
        offset = anchor - FRONT_ANCHORS[0]
        rect = [
            FRONT_CONTROLLER[0] + offset,
            FRONT_CONTROLLER[1],
            FRONT_CONTROLLER[0] + offset + controller_width,
            FRONT_CONTROLLER[3],
        ]
        controller_boxes.append({
            "type": "controller",
            "box": polygon(rect),
            "manual": True,
            "slot": slot,
        })

    pcb_boxes = []
    for marked_slot, marked_rect in enumerate(BACK_PCB_MARKS):
        runtime_slot = 3 - marked_slot
        pcb_boxes.append({
            "type": "pcb",
            "box": polygon(rotate_180(marked_rect)),
            "manual": True,
            "slot": runtime_slot,
        })
    pcb_boxes.sort(key=lambda box: box["slot"])

    front["boxes"] = controller_boxes
    back["boxes"] = pcb_boxes
    for layout in (front, back):
        for index, box in enumerate(layout["boxes"], 1):
            box["id"] = index

    template.update({
        "calibrated": True,
        "retired": False,
        "created": "2026-08-13",
        "note": (
            "2026-08-13当前四列托盘完整标定：槽1至槽4从左到右；"
            "DRAM每张照片使用规则OCR实时定位，背面按2列x10行几何网格补全漏位；"
            "PCB使用all_mb逐槽人工框；"
            "主控使用槽1人工框并按各槽金手指锚点映射。"
        ),
    })


def draw_overlay(template: dict, side: str, source: Path, target: Path) -> None:
    image = Image.open(source).convert("RGB")
    draw = ImageDraw.Draw(image)
    layout = template["sides"][side]
    colors = {"dram": "lime", "controller": "cyan", "pcb": "red"}

    for slot, rect in enumerate(layout["slots"], 1):
        pixels = [rect[0] * WIDTH, rect[1] * HEIGHT,
                  rect[2] * WIDTH, rect[3] * HEIGHT]
        draw.rectangle(pixels, outline="yellow", width=7)
        draw.text((pixels[0] + 10, pixels[1] + 10), f"S{slot}",
                  fill="yellow", stroke_width=2, stroke_fill="black")

    for box in layout["boxes"]:
        xs = [point[0] * WIDTH for point in box["box"]]
        ys = [point[1] * HEIGHT for point in box["box"]]
        draw.rectangle([min(xs), min(ys), max(xs), max(ys)],
                       outline=colors.get(box["type"], "white"), width=4)

    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, quality=95)


def main() -> None:
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    replace_fixed_boxes(template)
    TEMPLATE_PATH.write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    output = ROOT / "outputs" / "template_0085_complete"
    draw_overlay(template, "front", ROOT / "uploads" / "0085" / "front.jpg",
                 output / "front_complete_template.jpg")
    draw_overlay(template, "back", ROOT / "uploads" / "0085" / "back.jpg",
                 output / "back_complete_template.jpg")
    print(TEMPLATE_PATH)
    print(output)


if __name__ == "__main__":
    main()
