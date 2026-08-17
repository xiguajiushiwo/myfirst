from PIL import Image, ImageDraw

from app.recognition.region_ocr import detect_occupied_slots


def test_slot_presence_keeps_weak_real_slot_texture():
    image = Image.new("L", (200, 200), 0)
    draw = ImageDraw.Draw(image)
    for x in range(5, 200, 50):
        draw.line((x, 0, x, 199), fill=255, width=1)

    slots = detect_occupied_slots(image.convert("RGB"), [[0, 0, 1, 1]])

    assert slots[0]["score"] < 0.045
    assert slots[0]["score"] >= 0.035
    assert slots[0]["occupied"] is True
