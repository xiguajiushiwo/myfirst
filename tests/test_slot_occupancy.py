"""托盘槽位占位检测单测（先数数量、空位跳过）。

不依赖 OCR/GPU：用合成图（平坦=空槽、密纹=有条）验证边缘密度判空/满，
并验证槽位矩形取法（显式 slots 优先、否则按框 x 跨度自动聚类）与框→槽归属。
"""
import numpy as np
from PIL import Image

from app.recognition.region_ocr import (detect_occupied_slots,
                                         _slot_rects_for_layout,
                                         _auto_slot_rects, _slot_of_box,
                                         _box_kind, _SLOT_PRESENCE_MIN)


def _half_flat_half_textured(W=400, H=200):
    """左半平坦(空槽)、右半细棋盘(有条=大量边缘)的合成图。"""
    arr = np.full((H, W, 3), 128, dtype=np.uint8)
    xs = np.arange(W // 2, W)
    ys = np.arange(H)
    # 右半 4px 棋盘 → 边缘密集且确定（不用随机）
    for y in ys:
        for x in xs:
            if ((x // 4) + (y // 4)) % 2:
                arr[y, x] = 255
            else:
                arr[y, x] = 0
    return Image.fromarray(arr)


def _rect(box):
    return [box[0][0], box[0][1], box[2][0], box[2][1]]


def test_empty_vs_occupied_slot():
    """左半平坦 → 空位(score≈0)；右半密纹 → 有条(score 远超阈值)。"""
    img = _half_flat_half_textured()
    rects = [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 1.0, 1.0]]
    occ = detect_occupied_slots(img, rects)
    assert len(occ) == 2
    left, right = occ[0], occ[1]
    assert left["slot"] == 0 and right["slot"] == 1
    assert left["occupied"] is False and left["score"] < _SLOT_PRESENCE_MIN
    assert right["occupied"] is True and right["score"] > _SLOT_PRESENCE_MIN


def test_occupied_count():
    img = _half_flat_half_textured()
    rects = [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 1.0, 1.0]]
    occ = detect_occupied_slots(img, rects)
    assert sum(1 for o in occ if o["occupied"]) == 1     # 只有右槽有条


def test_explicit_slots_preferred_and_sorted():
    """模板有显式 slots → 直接用（按 x 左→右排序），不走自动聚类。"""
    layout = {"slots": [[0.6, 0, 0.9, 1], [0.1, 0, 0.4, 1]], "boxes": []}
    rects = _slot_rects_for_layout(layout)
    assert len(rects) == 2
    assert rects[0][0] < rects[1][0]                     # 左→右
    assert rects[0][0] == 0.1 and rects[1][0] == 0.6


def test_auto_cluster_two_sticks():
    """无显式 slots：两簇框(x 0.1~0.2 与 0.7~0.8) → 自动聚成 2 槽。"""
    def box(x0, x1, y0=0.1, y1=0.2):
        return {"box": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}
    boxes = [box(0.10, 0.12), box(0.13, 0.15), box(0.18, 0.20),
             box(0.70, 0.72), box(0.73, 0.75), box(0.78, 0.80)]
    rects = _auto_slot_rects(boxes)
    assert len(rects) == 2
    assert rects[0][2] < 0.5 < rects[1][0]               # 两槽被大间隙分开


def test_slot_of_box_containment_and_nearest():
    rects = [[0.0, 0.0, 0.4, 1.0], [0.6, 0.0, 1.0, 1.0]]
    inside0 = [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]]
    inside1 = [[0.7, 0.1], [0.8, 0.1], [0.8, 0.2], [0.7, 0.2]]
    assert _slot_of_box(inside0, rects) == 0
    assert _slot_of_box(inside1, rects) == 1
    # 落在两槽之间(0.5) → 取 x 最近的槽（此处槽0中心0.2、槽1中心0.8，0.5更近槽0）
    between = [[0.48, 0.1], [0.52, 0.1], [0.52, 0.2], [0.48, 0.2]]
    assert _slot_of_box(between, rects) == 0


def test_no_slots_returns_empty():
    assert _slot_rects_for_layout({"boxes": []}) == []


def test_box_kind_white_label_vs_textured_chip():
    """白标签（又白又平）→ 'label'（跳过，别把标签数字当日期）；密纹芯片 → 'chip'。"""
    # 纯白框区 → 标签
    white = Image.fromarray(np.full((80, 160, 3), 255, dtype=np.uint8))
    box = [[0, 0], [160, 0], [160, 80], [0, 80]]      # 覆盖整张的框
    assert _box_kind(white, box) == "label"
    # 细棋盘（黑塑封+密集小字的近似）→ 芯片
    chip = _half_flat_half_textured(160, 80)          # 右半密纹
    box_r = [[80, 0], [160, 0], [160, 80], [80, 80]]
    assert _box_kind(chip, box_r) == "chip"
