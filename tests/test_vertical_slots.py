from PIL import Image

from app.recognition.region_ocr import (_slot_of_box, _slot_rects_for_layout,
                                         detect_occupied_slots)


def test_vertical_slots_are_sorted_top_to_bottom():
    layout = {
        "slot_axis": "vertical",
        "slots": [
            [0.1, 0.55, 0.9, 0.75],
            [0.1, 0.05, 0.9, 0.25],
            [0.1, 0.80, 0.9, 0.98],
            [0.1, 0.30, 0.9, 0.50],
        ],
    }
    slots = _slot_rects_for_layout(layout)
    assert [slot[1] for slot in slots] == [0.05, 0.30, 0.55, 0.80]
    box = [[0.2, 0.60], [0.3, 0.60], [0.3, 0.65], [0.2, 0.65]]
    assert _slot_of_box(box, slots) == 2


def test_vertical_occupancy_keeps_top_to_bottom_numbering():
    image = Image.new("RGB", (1000, 1000), "black")
    slots = [[0, 0.5, 1, 0.75], [0, 0, 1, 0.25], [0, 0.75, 1, 1], [0, 0.25, 1, 0.5]]
    result = detect_occupied_slots(image, slots, thr=0, axis="vertical")
    assert [item["box"][1] for item in result] == [0, 250, 500, 750]


def test_outside_box_uses_nearest_vertical_slot():
    slots = [[0.1, 0.05, 0.9, 0.25], [0.1, 0.30, 0.9, 0.50],
             [0.1, 0.55, 0.9, 0.75], [0.1, 0.80, 0.9, 0.98]]
    box = [[0.95, 0.58], [0.99, 0.58], [0.99, 0.62], [0.95, 0.62]]
    assert _slot_of_box(box, slots) == 2
