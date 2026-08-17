from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 4024, 3036
TEMPLATE_ID = "samsung-vertical-4up-20260813"
SLOTS = {
    "front": [[0.17, 0.025, 0.94, 0.245], [0.17, 0.265, 0.94, 0.505],
              [0.17, 0.520, 0.94, 0.755], [0.17, 0.770, 0.94, 1.000]],
    "back": [[0.16, 0.000, 0.95, 0.215], [0.16, 0.245, 0.95, 0.470],
             [0.16, 0.515, 0.95, 0.750], [0.16, 0.765, 0.95, 1.000]],
}

FRONT_DRAM = [
    (0, 803, 82, 44, 36), (0, 1048, 82, 53, 38), (0, 1301, 85, 53, 37),
    (0, 1562, 85, 52, 38), (0, 1822, 88, 40, 37), (0, 2431, 242, 44, 35),
    (1, 800, 823, 51, 50), (1, 1045, 821, 62, 53), (1, 1306, 826, 44, 49),
    (1, 1567, 823, 44, 50), (1, 1823, 826, 42, 48), (1, 2456, 823, 44, 49),
    (1, 2710, 826, 42, 49), (1, 2964, 823, 42, 49), (1, 3218, 826, 54, 52),
    (1, 3473, 826, 53, 52), (1, 3466, 1043, 45, 48),
    (2, 803, 1616, 43, 47), (2, 1057, 1611, 52, 50), (2, 1313, 1611, 44, 47),
    (2, 1567, 1613, 44, 48), (2, 1826, 1613, 44, 48), (2, 2454, 1611, 45, 48),
    (2, 2704, 1611, 44, 47), (2, 2962, 1611, 46, 48), (2, 3224, 1616, 44, 47),
    (2, 3474, 1618, 44, 48), (2, 3469, 1826, 44, 48),
    (3, 813, 2585, 43, 34), (3, 1059, 2576, 53, 36), (3, 1309, 2577, 60, 37),
    (3, 1572, 2576, 44, 35), (3, 1827, 2589, 30, 51), (3, 2449, 2577, 43, 34),
    (3, 2701, 2577, 43, 34), (3, 2961, 2582, 43, 34), (3, 3211, 2582, 43, 33),
    (3, 3451, 2582, 63, 35), (3, 3464, 2728, 43, 34),
]

BACK_DRAM = [
    (0, x, y, w, h) for x, y, w, h in [
        (911,443,62,38),(1179,440,62,38),(1451,441,44,36),(1715,438,43,35),(1976,440,53,37),
        (2594,435,63,38),(2869,437,46,36),(3123,437,61,38),(3386,435,62,38),(3655,440,61,38),
        (905,592,53,38),(1172,590,45,35),(1437,590,45,35),(1693,587,53,37),(1967,586,45,35),
        (2615,584,45,35),(2879,584,55,37),(3148,586,45,35),(3408,587,61,39),(3669,588,61,40)]] + [
    (1, x, y, w, h) for x, y, w, h in [
        (903,1142,62,50),(1174,1139,46,46),(1442,1136,45,47),(1706,1139,44,46),(1975,1138,45,47),
        (2614,1135,41,46),(2878,1138,44,45),(3148,1141,45,46),(3403,1134,62,50),(3674,1140,54,49),
        (903,1348,44,44),(1153,1345,63,50),(1420,1345,54,49),(1694,1347,45,45),(1959,1344,53,48),
        (2624,1344,46,46),(2888,1349,53,49),(3158,1346,45,46),(3424,1348,44,46),(3688,1347,45,47)]] + [
    (2, x, y, w, h) for x, y, w, h in [
        (909,1971,46,48),(1176,1973,46,48),(1437,1971,45,49),(1709,1976,44,47),(1973,1972,45,49),
        (2614,1973,44,48),(2874,1972,55,51),(3144,1974,55,52),(3408,1971,62,53),(3679,1970,53,52),
        (905,2184,45,48),(1164,2188,45,48),(1420,2186,45,47),(1696,2191,46,49),(1957,2191,44,49),
        (2624,2192,45,48),(2890,2191,46,49),(3158,2194,45,49),(3416,2188,62,50),(3688,2190,45,50)]] + [
    (3, x, y, w, h) for x, y, w, h in [
        (903,2596,63,38),(1180,2600,46,34),(1439,2605,43,36),(1704,2608,45,34),(1972,2608,46,36),
        (2608,2611,44,36),(2862,2610,62,39),(3133,2612,55,38),(3401,2606,54,36),(3664,2604,63,38),
        (908,2743,62,37),(1168,2748,63,37),(1428,2753,54,36),(1699,2752,45,34),(1961,2758,47,34),
        (2615,2761,45,35),(2868,2759,62,39),(3143,2758,54,38),(3409,2753,55,36),(3664,2752,63,38)]]

FIXED = {
    "front": [(slot, "controller", [0.515, y0, 0.585, y1]) for slot, y0, y1 in
              [(0, .110, .185), (1, .355, .430), (2, .610, .685), (3, .855, .930)]],
    "back": [(slot, "pcb", [0.505, y0, 0.565, y1]) for slot, y0, y1 in
             [(0, .050, .095), (1, .300, .345), (2, .565, .610), (3, .820, .865)]],
}


def polygon(rect):
    x0, y0, x1, y1 = rect
    return [[round(x0, 4), round(y0, 4)], [round(x1, 4), round(y0, 4)],
            [round(x1, 4), round(y1, 4)], [round(x0, 4), round(y1, 4)]]


def dram_box(slot, x, y, width, height):
    center_x, center_y = x + width / 2, y + height / 2
    chip_width, chip_height = 205, 190
    return {"type": "dram", "box": polygon(((center_x - chip_width / 2) / WIDTH,
                                               (center_y - chip_height / 2) / HEIGHT,
                                               (center_x + chip_width / 2) / WIDTH,
                                               (center_y + chip_height / 2) / HEIGHT)),
            "manual": True, "slot": slot}


def build_side(side, rows):
    boxes = [dram_box(*row) for row in rows]
    boxes.extend({"type": kind, "box": polygon(rect), "manual": True, "slot": slot}
                 for slot, kind, rect in FIXED[side])
    for index, box in enumerate(boxes, 1):
        box["id"] = index
    return {"image_size": [WIDTH, HEIGHT], "slot_axis": "vertical",
            "dram_box_mode": "chip", "dram_rotation": "auto90",
            "slots": SLOTS[side], "boxes": boxes}


def draw_overlay(side, layout):
    source = ROOT / "client_data" / "template_rebuild" / f"{side}_current.jpg"
    image = Image.open(source).convert("RGB")
    draw = ImageDraw.Draw(image)
    for index, rect in enumerate(layout["slots"], 1):
        box = [rect[0] * WIDTH, rect[1] * HEIGHT, rect[2] * WIDTH, rect[3] * HEIGHT]
        draw.rectangle(box, outline="yellow", width=7)
        draw.text((box[0] + 8, box[1] + 8), str(index), fill="yellow", stroke_width=2, stroke_fill="black")
    for item in layout["boxes"]:
        xs = [point[0] * WIDTH for point in item["box"]]
        ys = [point[1] * HEIGHT for point in item["box"]]
        draw.rectangle([min(xs), min(ys), max(xs), max(ys)],
                       outline="red" if item["type"] != "dram" else "lime", width=4)
    image.save(ROOT / "client_data" / "template_rebuild" / f"{side}_new_template_overlay.jpg", quality=94)


def main():
    sides = {"front": build_side("front", FRONT_DRAM),
             "back": build_side("back", BACK_DRAM)}
    template = {
        "id": TEMPLATE_ID,
        "brand": "Samsung",
        "model": "Samsung 64GB DDR5-5600 RDIMM vertical 4up",
        "capacity": "64GB",
        "frequency": "5600",
        "calibrated": True,
        "slot_axis": "vertical",
        "created": "2026-08-13",
        "note": "适用于当前纵向四槽托盘；正面不旋转、反面旋转180度；槽1到槽4从上到下。",
        "sides": sides,
    }
    target = ROOT / "app" / "recognition" / "templates" / f"{TEMPLATE_ID}.json"
    target.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    for side, layout in sides.items():
        draw_overlay(side, layout)
    print(target)


if __name__ == "__main__":
    main()
