# -*- coding: utf-8 -*-
"""几何定位 + 逐框识别（`mode="geo"`）。**与模板/规则两条老路完全并行，互不影响。**

为什么另开一条路而不改原有的：
  `recognize_side` 吃模板里的归一化坐标、`recognize_rules` 整图找日期，
  两条都在跑着的产线上验证过。几何定位换的是"框从哪来"这一层根基，
  混进去改必然牵动那两条。所以新增独立入口，原路一行不动、随时可切回。

与模板模式的关键差别：
  框**每张图现算**（`geo_locate`），不存坐标 → 托盘位置变了不会滑框。
  已知问题里 slot1/slot3 的 PCB 被读成 2517/2628（应 2543）就是滑框造成的。

按型别分开预处理 —— 这三处的最优参数实测**互相冲突**，不能一套通吃：
  颗粒(dram)  CLAHE 增强一次即可，实测槽1 20/20。
  PCB         **不增强 + 2× 放大 + 试正/倒两个方向**。增强实测有害：
              旁边料号 QRG1720HP 的 1720 会被增强得更醒目从而抢读，
              槽2 增强到 2~6× 直接把那行毁掉读成"无行"。
              两方向是必须的 —— 实测同一盘内槽1/2 正印、槽3/4 倒印 180°，
              固定按一个方向处理必然漏掉一半。
  PMIC        **强制 2× 放大**。1× 时四槽全空，2× 起 4/4 置信 0.99~1.00。
  SOT         实测 OCR 0/4（字高仅 18px，在 rec 架构分辨率下限上，
              放大到 6× 也没用）→ **直接交大模型兜底**，读不出就是盲点。
"""
from __future__ import annotations

import datetime
import logging
import os
import tempfile
from statistics import median
from typing import Optional

import numpy as np
from PIL import Image, ImageOps

log = logging.getLogger("yxq.geo")
from .date_parser import (DateCode, _decode_yww, _decode_yyww, _week_start_date)
from .ocr_engine import get_component_engine
from .region_ocr import (_CONF_MIN, _box_kind, _bbox, _crop_region_geo, _enhance,
                         _eval_crop, _read_dram, _read_yyww, _roi_local,
                         _physical_slot_for_visual, _slot_rects_for_layout,
                         _tight_box_from_hit, _vl_fallback_yyww,
                         recognize_rules)

# PCB 倒读也可能解出**结构合法**的假值：实测槽1 正读 2536(置信1.00)、
# 倒读 2025(置信0.21)，而 2025 也能通过年/周合法性检查。所以只靠"解出来了"
# 不足以定方向，得看置信差。两向置信都高且接近时**不猜** —— 标盲点转人工。
_ROT_AMBIG_RATIO = float(os.environ.get("GEO_ROT_AMBIG_RATIO", "2.0"))
_ROT_STRONG_SCORE = float(os.environ.get("GEO_ROT_STRONG_SCORE", "0.98"))
_ROT_WEAK_SCORE_MAX = float(os.environ.get("GEO_ROT_WEAK_SCORE_MAX", "0.90"))
_PCB_FAST_SCORE = float(os.environ.get("GEO_PCB_FAST_SCORE", "0.85"))


def _scaled(crop: Image.Image, f: int) -> Image.Image:
    """按整数倍放大。小字在 rec 归一化到高 48 时会被上采样，提前放大能多给几个时间步。"""
    if crop is None or f <= 1:
        return crop
    return crop.resize((crop.width * f, crop.height * f), Image.LANCZOS)


def _read_dram_box(engine, crop, roi, year):
    """颗粒：CLAHE 增强读一次（与 region_ocr._best_read 同口径）。"""
    y, w, raw, score, joined, _ts, hit = _eval_crop(
        engine, _enhance(crop), year, _read_dram, roi)
    return y, w, raw, score, joined, hit, ""


def _read_pmic_box(engine, crop, roi, year):
    """PMIC：强制 2× 放大。roi 也要跟着放大，否则排序用的坐标系和图不一致。"""
    y, w, raw, score, joined, _ts, hit = _eval_crop(
        engine, _scaled(crop, 2), year, _read_yyww,
        tuple(v * 2 for v in roi) if roi else None)
    return y, w, raw, score, joined, hit, "2×放大"


def _restore_scaled_hit(hit, size: tuple[int, int], rotation: int):
    """Map a hit from the 2x OCR image back into the original crop coordinates."""
    if not hit or not hit[1]:
        return hit
    text, box = hit
    width, height = size
    if rotation == 180:
        box = [[width - point[0], height - point[1]] for point in box]
    return text, [[point[0] / 2, point[1] / 2] for point in box]


def _clash(dc, others) -> Optional[object]:
    """本框的读数是不是**别处那一框**的？返回冲突对象或 None。

    为什么要这道检查：PMIC 的框纵向仍是固定比例、偏大，实测槽1 的 PMIC 框
    把 PCB 日期码（y 1412..1441）圈了进去，于是读出 2536 —— 与 PCB 一字不差、
    置信 0.999。按铁律，把一处的日期安到另一处头上就是漏判，
    比读不出更危险（读不出会转人工，读串了会当合格放过）。

    判据：同槽、不同型别、读数字符串相同，且两框**在纵向有重叠**
    （纯粹凑巧读数相同不算 —— 同盘同批次本来就可能同日期，所以必须要求框重叠，
    这才说明是同一行字被两个框都圈到了）。
    """
    if not dc.raw or not dc.box:
        return None
    ys = [p[1] for p in dc.box]
    a0, a1 = min(ys), max(ys)
    for o in others:
        if o is dc or o.code_type == dc.code_type or o.slot != dc.slot:
            continue
        if not o.raw or o.raw != dc.raw or not o.box:
            continue
        oy = [p[1] for p in o.box]
        if min(oy) < a1 and a0 < max(oy):        # 纵向重叠 → 同一行字被两框圈到
            return o
    return None


def _read_pcb_box(engine, crop, year, fast: bool = False,
                  preferred_rotation: Optional[int] = None):
    """PCB：不增强、2× 放大、试正/倒两向，按置信取。返回值末位是 note。

    不传 roi：PCB 框已经收窄到日期码本身（条宽的 12%、高 49px），
    框内没有邻居可混，而翻转后 roi 坐标要跟着折算，徒增出错面。

    两向都解出且置信接近 → 不猜，note 里标明需人工确认（调用方据此转盲点）。
    """
    def recent(y):
        return bool(y) and ((year - 12) <= y <= (year + 1))

    def read_variant(source, rot):
        im = source.rotate(180) if rot else source
        y, w, raw, score, joined, _ts, hit = _eval_crop(
            engine, im, year, _read_yyww, None)
        return y, w, raw, score, joined, hit

    def key_rot(key):
        return key[1] if isinstance(key, tuple) else key

    def rotation_order():
        if preferred_rotation in (0, 180):
            return [preferred_rotation, 180 - preferred_rotation]
        return [0, 180]

    def restore_hit(hit, key):
        if isinstance(key, tuple) and key[0] == "color":
            return None
        return _restore_scaled_hit(hit, base.size, key_rot(key))

    def orientation_note(key):
        prefix = ""
        if isinstance(key, tuple):
            prefix = {"gray": "灰度增强", "color": "颜色差分局部增强"}.get(key[0], "")
        suffix = "倒印180°" if key_rot(key) == 180 else ""
        if prefix and suffix:
            return prefix + "，" + suffix
        return prefix or suffix

    color_image = None
    color_tried: set[int] = set()

    def read_color(rot):
        nonlocal color_image
        if color_image is None:
            arr = np.asarray(crop.convert("RGB"))
            height, width = arr.shape[:2]
            roi = arr[round(height * 0.38):height, round(width * 0.32):width]
            red = roi[:, :, 0].astype("int16")
            green = roi[:, :, 1].astype("int16")
            silk = np.clip(red - green + 128, 0, 255).astype("uint8")
            silk = ImageOps.autocontrast(Image.fromarray(silk))
            color_image = silk.resize(
                (silk.width * 4, silk.height * 4), Image.LANCZOS).convert("RGB")
        color_tried.add(rot)
        y, w, raw, score, joined, hit = read_variant(color_image, rot)
        got[("color", rot)] = (y, w, raw, score, joined, hit)
        return y, w, raw, score, joined, hit

    base = _scaled(crop, 2)
    got = {}
    rotations = rotation_order()
    for index, rot in enumerate(rotations):
        y, w, raw, score, joined, hit = read_variant(base, rot)
        got[rot] = (y, w, raw, score, joined, hit)
        if fast and recent(y) and score >= _PCB_FAST_SCORE:
            return y, w, raw, score, joined, _restore_scaled_hit(hit, base.size, rot), \
                orientation_note(rot)
        if fast and preferred_rotation in (0, 180) and index == 0:
            y, w, raw, score, joined, hit = read_color(rot)
            if recent(y) and score >= _PCB_FAST_SCORE:
                return y, w, raw, score, joined, None, orientation_note(("color", rot))

    valid = [(rot, v) for rot, v in got.items() if v[0]]
    base_valid = [(rot, v) for rot, v in got.items()
                  if not isinstance(rot, tuple) and v[0]]
    if not base_valid:
        if fast and 0 not in color_tried:
            y, w, raw, score, joined, hit = read_color(0)
            if recent(y) and score >= _PCB_FAST_SCORE:
                return y, w, raw, score, joined, None, "颜色差分局部增强"
        gray = _scaled(crop.convert("L").convert("RGB"), 2)
        for rot in rotations:
            y, w, raw, score, joined, hit = read_variant(gray, rot)
            got[("gray", rot)] = (y, w, raw, score, joined, hit)
            if fast and recent(y) and score >= _PCB_FAST_SCORE:
                return y, w, raw, score, joined, _restore_scaled_hit(hit, gray.size, rot), \
                    ("灰度增强，倒印180°" if rot else "灰度增强")
        valid = [(rot, v) for rot, v in got.items() if v[0]]
    if not valid:
        for rot in rotations:
            if rot in color_tried:
                continue
            y, w, raw, score, joined, hit = read_color(rot)
            if fast and recent(y) and score >= _PCB_FAST_SCORE:
                note = "颜色差分局部增强" + ("，倒印180°" if rot else "")
                return y, w, raw, score, joined, None, note
        valid = [(rot, v) for rot, v in got.items() if v[0]]
        if valid:
            rot, (y, w, raw, score, joined, _hit) = max(
                valid, key=lambda item: item[1][3])
            note = "颜色差分局部增强" + ("，倒印180°" if rot else "")
            return y, w, raw, score, joined, None, note
    if not valid:
        rot = rotations[0]
        y, w, raw, score, joined, hit = got[rot]
        return y, w, raw, score, joined, _restore_scaled_hit(hit, base.size, rot), \
            orientation_note(rot)
    if len(valid) == 1:
        rot, (y, w, raw, score, joined, hit) = valid[0]
        return y, w, raw, score, joined, restore_hit(hit, rot), orientation_note(rot)
    # 两向都解出：取置信高者，并判是否构成歧义
    (r_hi, hi), (r_lo, lo) = sorted(valid, key=lambda kv: -kv[1][3])
    y, w, raw, score, joined, hit = hi
    lo_y, lo_w, lo_raw, lo_score = lo[0], lo[1], lo[2], lo[3]
    same_date = (y, w) == (lo_y, lo_w)
    strong_winner = score >= _ROT_STRONG_SCORE and lo_score <= _ROT_WEAK_SCORE_MAX
    ambig = (not same_date and lo_score > 0
             and score < lo_score * _ROT_AMBIG_RATIO and not strong_winner)
    note = (f"正/倒两向都解出且置信接近（{raw}@{score:.2f} vs {lo_raw}@{lo_score:.2f}），"
            f"无法定向，请人工确认"
            if ambig else
            (f"正/倒两向一致为 {raw}，取高置信结果 {score:.2f}"
             if same_date else
             f"两向都解出，取置信高者 {raw}@{score:.2f}（另一向 {lo_raw}@{lo_score:.2f}）"))
    return (None if ambig else y), (None if ambig else w), raw, score, joined, \
        (None if ambig else restore_hit(hit, r_hi)), note


def _read_controller_box(engine, crop, year, fast: bool = False,
                         preferred_rotation: Optional[int] = None):
    """主控/RCD 与 PCB 都使用四位 YYWW，并同时尝试正向和 180 度。"""
    return _read_pcb_box(engine, crop, year, fast=fast,
                         preferred_rotation=preferred_rotation)


# 每种型别：DateCode.code_type、位数格式、读法、是否参与逐颗日期比对
_KINDS = {
    "dram": ("dram", "YWW", _read_dram_box),
    "controller": ("controller", "YYWW", _read_controller_box),
    "pcb":  ("pcb", "YYWW", None),          # 特殊：_read_pcb_box 不吃 roi
    "pmic": ("pmic", "YYWW", _read_pmic_box),
    "sot":  ("sot", "YYWW", None),          # 特殊：OCR 读不动，直接送大模型
}


def _mk(kind: str, raw, y, w, score, joined, box_px, note, status) -> DateCode:
    ct, fmt, _ = _KINDS[kind]
    return DateCode(raw=raw, code_type=ct, year=y or 0, week=w or 0,
                    week_start=_week_start_date(y, w) if y else "",
                    confidence=score, source_text=joined, box=box_px,
                    digit_format=fmt, status=status, note=note,
                    ocr_raw=raw, ocr_confidence=score)


def _tpl_fixed(side: str, template_id: Optional[str]) -> dict:
    """从模板取**人工标定**的 PCB/主控 框 → {槽号(1起): {"pcb"/"controller": [x0,y0,x1,y1]}}。

    只取 manual=True 的框（自动生成的颗粒框不在这里用，颗粒仍走现算）。
    模板缺失/无此类框 → 返回 {}，调用方回退原来的现算逻辑，不会崩。
    """
    try:
        from . import template_store
        tid = template_id or template_store.default_template_id()
        tpl = template_store.get_template(tid) if tid else None
        layout = ((tpl or {}).get("sides") or {}).get(side) or {}
        out: dict = {}
        slot_count = len(_slot_rects_for_layout(layout))
        for b in layout.get("boxes") or []:
            kind = b.get("type")
            if kind not in ("pcb", "controller", "pmic", "sot") or not b.get("manual"):
                continue
            xs = [p[0] for p in b["box"]]
            ys = [p[1] for p in b["box"]]
            visual_slot = int(b.get("slot", 0))
            slot1 = _physical_slot_for_visual(layout, visual_slot, slot_count) + 1
            out.setdefault(slot1, {})[kind] = [min(xs), min(ys), max(xs), max(ys)]
        return out
    except Exception as e:  # noqa: BLE001
        log.info("读模板人工框失败(回退现算)：%s", e)
        return {}


def _tpl_layout(side: str, template_id: Optional[str]) -> dict:
    try:
        from . import template_store
        tid = template_id or template_store.default_template_id()
        tpl = template_store.get_template(tid) if tid else None
        return (((tpl or {}).get("sides") or {}).get(side) or {})
    except Exception as exc:  # noqa: BLE001
        log.info("读取存储芯片槽位布局失败: %s", exc)
        return {}


def _layout_rotation(layout: dict, kind: str, slot_index: int) -> Optional[int]:
    """Read optional template rotation hints for tiny PCB/controller OCR boxes."""
    raw = layout.get(f"{kind}_primary_rotations")
    if isinstance(raw, dict):
        value = raw.get(str(slot_index + 1), raw.get(str(slot_index)))
    elif isinstance(raw, list) and 0 <= slot_index < len(raw):
        value = raw[slot_index]
    else:
        value = layout.get(f"{kind}_primary_rotation")
    try:
        rotation = int(value)
    except (TypeError, ValueError):
        return None
    return rotation if rotation in (0, 180) else None


def _rotate_box_180(box: list, width: int, height: int) -> list:
    return [[width - point[0], height - point[1]] for point in box]


def _norm_box_to_rect(box: list, width: int, height: int) -> list[float]:
    xs = [float(point[0]) for point in box]
    ys = [float(point[1]) for point in box]
    return [min(xs) * width, min(ys) * height, max(xs) * width, max(ys) * height]


def _expand_rect(rect: list[float], width: int, height: int,
                 pad_x: float = 0.55, pad_y: float = 1.2) -> list[float]:
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    return [
        max(0.0, x0 - w * pad_x),
        max(0.0, y0 - h * pad_y),
        min(float(width), x1 + w * pad_x),
        min(float(height), y1 + h * pad_y),
    ]


def _component_exclusion_rects(layout: dict, width: int, height: int) -> list[list[float]]:
    rects = []
    for item in layout.get("boxes") or []:
        if item.get("type") not in ("pcb", "controller", "pmic", "sot"):
            continue
        box = item.get("box") or []
        if len(box) < 4:
            continue
        rects.append(_expand_rect(
            _norm_box_to_rect(box, width, height), width, height,
            pad_x=0.15, pad_y=0.25))
    return rects


def _label_exclusion_rects(image: Image.Image, layout: dict) -> list[list[float]]:
    arr = np.asarray(image.convert("RGB"))
    height, width = arr.shape[:2]
    if not width or not height:
        return []

    slot_rects = []
    for rect in _slot_rects_for_layout(layout):
        x0, y0, x1, y1 = rect
        slot_rects.append([x0 * width, y0 * height, x1 * width, y1 * height])

    try:
        import cv2
        import zxingcpp

        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        barcode_rects = []
        for code in zxingcpp.read_barcodes(bgr):
            if "(S)" not in (code.text or ""):
                continue
            try:
                pos = code.position
                points = [
                    (pos.top_left.x, pos.top_left.y),
                    (pos.top_right.x, pos.top_right.y),
                    (pos.bottom_right.x, pos.bottom_right.y),
                    (pos.bottom_left.x, pos.bottom_left.y),
                ]
            except Exception:  # noqa: BLE001
                continue
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            qr_w = max(1.0, x1 - x0)
            qr_h = max(1.0, y1 - y0)
            rect = [
                max(0.0, x0 - max(qr_w * 0.45, width * 0.012)),
                max(0.0, y0 - max(qr_h * 5.8, height * 0.18)),
                min(float(width), x1 + max(qr_w * 0.45, width * 0.012)),
                min(float(height), y1 + max(qr_h * 0.55, height * 0.018)),
            ]
            center = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
            if slot_rects and not any(_inside_rect(center, slot) for slot in slot_rects):
                continue
            barcode_rects.append(rect)
        if barcode_rects:
            return barcode_rects
    except Exception:  # noqa: BLE001
        pass

    try:
        import cv2
    except Exception:  # noqa: BLE001
        return []

    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    mask = ((hsv[:, :, 2] >= 150) & (hsv[:, :, 1] <= 95)).astype("uint8") * 255
    kernel_w = max(5, width // 260)
    kernel_h = max(9, height // 180)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((kernel_h, kernel_w), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = width * height * 0.002
    rects = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w * h < min_area:
            continue
        if w < width * 0.015 or h < height * 0.10:
            continue
        if h < w * 1.35:
            continue
        if w > width * 0.16:
            continue
        center = (x + w / 2, y + h / 2)
        if slot_rects and not any(_inside_rect(center, rect) for rect in slot_rects):
            continue
        pad_x = max(18.0, width * 0.012)
        pad_y = max(18.0, height * 0.012)
        rects.append([
            max(0.0, x - pad_x),
            max(0.0, y - pad_y),
            min(float(width), x + w + pad_x),
            min(float(height), y + h + pad_y),
        ])
    return rects


def _box_center_px(box: list) -> tuple[float, float] | None:
    if not box:
        return None
    return (sum(float(point[0]) for point in box) / len(box),
            sum(float(point[1]) for point in box) / len(box))


def _inside_rect(point: tuple[float, float] | None, rect: list[float]) -> bool:
    if point is None:
        return False
    x, y = point
    x0, y0, x1, y1 = rect
    return x0 <= x <= x1 and y0 <= y <= y1


def _cluster_axis(values: list[float], count: int) -> list[float]:
    if not values or count <= 0:
        return []
    ordered = sorted(values)
    if len(ordered) < count:
        return ordered
    gaps = [(ordered[index + 1] - ordered[index], index)
            for index in range(len(ordered) - 1)]
    cuts = sorted(index for _gap, index in
                  sorted(gaps, reverse=True)[:count - 1])
    groups = []
    start = 0
    for cut in cuts:
        groups.append(ordered[start:cut + 1])
        start = cut + 1
    groups.append(ordered[start:])
    return [median(group) for group in groups if group]


def _read_completed_dram_cell(code: DateCode, image: Image.Image,
                              current_year: int) -> None:
    if not code.box:
        return
    xs = [point[0] for point in code.box]
    ys = [point[1] for point in code.box]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    engine = get_component_engine()
    for pad in (8, 16, 28, 44, 0):
        ox = max(0, int(x0 - pad))
        oy = max(0, int(y0 - pad))
        rx = min(image.width, int(x1 + pad))
        by = min(image.height, int(y1 + pad))
        if rx <= ox or by <= oy:
            continue
        crop = image.crop((ox, oy, rx, by))
        y, w, raw, score, joined, hit, _note = _read_dram_box(
            engine, crop, None, current_year)
        if not y or not w or score < 0.60:
            continue
        code.raw = raw
        code.year = y
        code.week = w
        code.week_start = _week_start_date(y, w)
        code.confidence = score
        code.source_text = joined
        code.status = "ok"
        code.note = "局部OCR补读：整图未定位，按反面2x10网格补框后读出"
        code.ocr_raw = raw
        code.ocr_confidence = score
        if hit and hit[1]:
            code.box = [[point[0] + ox, point[1] + oy] for point in hit[1]]
        return


def _complete_back_grid(drams: list[DateCode], slot_rects: list,
                        width: int, height: int,
                        image: Optional[Image.Image] = None,
                        current_year: Optional[int] = None) -> list[DateCode]:
    """Complete the physical 2x10 grid without inventing missing dates."""
    if len(slot_rects) != 4:
        return drams
    centers = []
    box_widths = []
    box_heights = []
    for code in drams:
        if code.slot < 0 or not code.box:
            continue
        xs = [point[0] for point in code.box]
        ys = [point[1] for point in code.box]
        centers.append((sum(xs) / len(xs), sum(ys) / len(ys)))
        box_widths.append(max(xs) - min(xs))
        box_heights.append(max(ys) - min(ys))
    if len(centers) < 60:
        return drams

    columns = _cluster_axis([point[0] for point in centers], 8)
    rows = _cluster_axis([point[1] for point in centers], 10)
    if len(columns) != 8 or len(rows) != 10:
        return drams
    box_width = median(box_widths)
    box_height = median(box_heights)

    for slot in range(4):
        present = [code for code in drams if code.slot == slot and code.box]
        missing_count = max(0, 20 - len(present))
        if missing_count == 0:
            continue
        used: set[tuple[int, int]] = set()
        for code in present:
            center_x = sum(point[0] for point in code.box) / len(code.box)
            center_y = sum(point[1] for point in code.box) / len(code.box)
            col = min(range(2), key=lambda i: abs(center_x - columns[slot * 2 + i]))
            row = min(range(10), key=lambda i: abs(center_y - rows[i]))
            used.add((col, row))
        candidates = []
        for col in range(2):
            for row in range(10):
                if (col, row) in used:
                    continue
                candidates.append((col, row))
        for col, row in candidates[:missing_count]:
                center_x = columns[slot * 2 + col]
                center_y = rows[row]
                half_width = box_width / 2
                half_height = box_height / 2
                box = [[center_x - half_width, center_y - half_height],
                       [center_x + half_width, center_y - half_height],
                       [center_x + half_width, center_y + half_height],
                       [center_x - half_width, center_y + half_height]]
                code = _mk("dram", "", None, None, 0.0, "", box,
                           "规则未定位到该存储芯片日期，请人工复核", "unknown")
                code.slot = slot
                if image is not None and current_year is not None:
                    _read_completed_dram_cell(code, image, current_year)
                drams.append(code)
    return drams


def _assign_dynamic_slots(drams: list[DateCode], slot_rects: list,
                          width: int, height: int, axis: str) -> None:
    coordinate = 1 if axis == "vertical" else 0
    dimension = height if axis == "vertical" else width

    def slot_at(coord: float) -> int:
        if not slot_rects:
            return -1
        index = 1 if axis == "vertical" else 0
        for slot_index, rect in enumerate(slot_rects):
            lo, hi = rect[index], rect[index + 2]
            if lo <= coord < hi or (slot_index == len(slot_rects) - 1 and lo <= coord <= hi):
                return slot_index
        return min(range(len(slot_rects)),
                   key=lambda i: abs(coord - (slot_rects[i][index] + slot_rects[i][index + 2]) / 2))

    centers = []
    for code in drams:
        if code.box:
            centers.append(sum(point[coordinate] for point in code.box) /
                           len(code.box) / dimension)
    chip_lanes = _cluster_axis(centers, len(slot_rects) * 2)
    if len(chip_lanes) == len(slot_rects) * 2:
        lane_slots = [slot_at(lane) for lane in chip_lanes]
        for code in drams:
            center = sum(point[coordinate] for point in code.box) / len(code.box) / dimension
            lane = min(range(len(chip_lanes)),
                       key=lambda index: abs(center - chip_lanes[index]))
            code.slot = lane_slots[lane]
        return
    for code in drams:
        center = sum(point[coordinate] for point in code.box) / len(code.box) / dimension
        code.slot = slot_at(center)


def recognize_dynamic_drams(image_path: str, side: str,
                            current_year: int,
                            template_id: Optional[str]) -> list[DateCode]:
    """Locate date text on the current frame instead of reusing DRAM boxes."""
    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    layout = _tpl_layout(side, template_id)
    slot_rects = _slot_rects_for_layout(layout)
    if not slot_rects:
        log.warning("%s面没有槽位布局，无法给动态存储芯片分槽", side)
        return []

    if side == "back":
        with tempfile.TemporaryDirectory(prefix="yxq-dram-") as directory:
            upright_path = os.path.join(directory, "upright.jpg")
            image.rotate(180).save(upright_path, quality=98)
            tile_bands = int(layout.get("dram_tile_bands", 3) or 3)
            codes = recognize_rules(upright_path, current_year=current_year,
                                    side=side, tile_bands=tile_bands)
        for code in codes:
            if code.box:
                code.box = _rotate_box_180(code.box, width, height)
    else:
        tile_bands = int(layout.get("dram_tile_bands", 3) or 3)
        codes = recognize_rules(image_path, current_year=current_year, side=side,
                                tile_bands=tile_bands)

    excluded = _component_exclusion_rects(layout, width, height)
    excluded.extend(_label_exclusion_rects(image, layout))
    drams = []
    for code in codes:
        if code.code_type != "dram" or not code.box:
            continue
        center = _box_center_px(code.box)
        if any(_inside_rect(center, rect) for rect in excluded):
            continue
        drams.append(code)
    axis = "vertical" if layout.get("slot_axis") == "vertical" else "horizontal"
    _assign_dynamic_slots(drams, slot_rects, width, height, axis)
    if side == "back":
        _complete_back_grid(drams, slot_rects, width, height, image, current_year)
    slot_count = len(slot_rects)
    for code in drams:
        if code.slot >= 0:
            code.slot = _physical_slot_for_visual(layout, code.slot, slot_count)
    for slot in range(len(slot_rects)):
        rows = [code for code in drams if code.slot == slot]
        rows.sort(key=lambda code: (
            min((point[1] for point in (code.box or [])), default=0),
            min((point[0] for point in (code.box or [])), default=0)))
        for index, code in enumerate(rows, 1):
            code.idx = index
    return drams


def recognize_geo(image_path: str, current_year: Optional[int] = None,
                  loc_out: Optional[dict] = None,
                  side: str = "back", template_id: Optional[str] = None) -> list[DateCode]:
    """几何定位 + 逐框识别一整盘。返回 [DateCode]，`slot` 按模板槽位顺序填好（0 起）。

    loc_out：给了就把 `geo_locate.locate` 的原始定位结果写进去（供上层标注/诊断）。

    盲点口径与铁律一致：读不出的框照实留 status="raw"/年周为 0，
    由 `services.compute_signal` 挑出来转人工 —— **不猜、不填、不表决**。
    """
    if current_year is None:
        current_year = datetime.date.today().year

    img = Image.open(image_path).convert("RGB")
    # PCB/主控直接使用模板里四槽人工标定框，不再依赖动态切条结果。
    # 大标签会让正面动态切条只找到一根，从而把另外三根主控静默漏掉；固定框则能明确标出遮挡。
    layout = _tpl_layout(side, template_id)
    fixed_norm = _tpl_fixed(side, template_id)
    loc = {"size": [img.width, img.height], "slots": [], "warn": []}
    for slot1, fixed in sorted(fixed_norm.items()):
        fixed_px = {
            kind: [int(box[0] * img.width), int(box[1] * img.height),
                   int(box[2] * img.width), int(box[3] * img.height)]
            for kind, box in fixed.items()
        }
        loc["slots"].append({"slot": slot1, "particles": [], "fixed": fixed_px})
    if not loc["slots"]:
        loc["warn"].append(f"{side} 面没有 PCB/主控人工标定框，请检查默认模板")
    if loc_out is not None:
        loc_out.clear()
        loc_out.update(loc)

    engine = get_component_engine()
    components: list[DateCode] = []
    pending4: list[tuple] = []                # PCB/主控 4位低置信项，统一标记人工复核

    for s in loc["slots"]:
        slot_i = s["slot"] - 1                # DateCode.slot 是 0 起
        # 正面检查主控/RCD，反面只检查 PCB。PMIC/SOT 不属于当前质检口径，不识别也不出结果。
        fixed_kinds = ("controller",) if side == "front" else ("pcb",)
        for kind in fixed_kinds:
            box = (s.get("fixed") or {}).get(kind)
            if not box:
                # 定不出框（颗粒不足 → 无中部带间隙）→ 照实出一个未读记录转人工，
                # 不静默跳过：静默跳过等于"这处没检查过"却看不出来。
                dc = _mk(kind, "", None, None, 0.0, "", None,
                         "几何定位未能定出框位，请人工确认", "unknown")
                dc.slot = slot_i
                components.append(dc)
                continue
            box_px = [[box[0], box[1]], [box[2], box[1]],
                      [box[2], box[3]], [box[0], box[3]]]
            if kind == "controller" and _box_kind(img, box_px) == "label":
                dc = _mk(kind, "", None, None, 0.0, "", box_px,
                         "主控日期区域被标签遮挡，无法识别，请人工确认", "covered")
                dc.slot = slot_i
                components.append(dc)
                continue
            x0, y0, x1, y1 = box
            if x1 <= x0 or y1 <= y0:
                continue
            # PCB/主控是人工精确标定框，禁止使用通用 60% padding；扩边会把旁边焊盘/料号
            # 带进 OCR，形成合法但错误的四位日期。小字放大由专用 reader 内部完成。
            crop = img.crop((x0, y0, x1, y1))
            ox, oy, cscale = x0, y0, 1.0
            preferred_rotation = _layout_rotation(layout, kind, slot_i)

            if kind == "pcb":
                y, w, raw, score, joined, hit, note = _read_pcb_box(
                    engine, crop, current_year,
                    fast=bool(layout.get("pcb_fast_read")),
                    preferred_rotation=preferred_rotation)
            else:
                y, w, raw, score, joined, hit, note = _read_controller_box(
                    engine, crop, current_year,
                    fast=bool(layout.get("controller_fast_read")),
                    preferred_rotation=preferred_rotation)
            score = round(score, 3)
            dc = _mk(kind, raw, y, w, score, joined, box_px,
                     note if y else (note or "OCR 未解码，请人工复核"),
                     "ok" if y else "raw")
            dc.slot = slot_i
            if y and hit and kind != "pcb":
                tb = _tight_box_from_hit(hit, raw, ox, oy, cscale)
                if tb:
                    dc.box = tb
            components.append(dc)
            # 年份离谱＝多半读到旁边元器件料号，哪怕置信高也要复核
            implausible = bool(y) and not ((current_year - 12) <= y <= (current_year + 1))
            if (not y) or (score < _CONF_MIN) or implausible:
                pending4.append((dc, crop))

    _vl_fallback_yyww(pending4, current_year)

    # 组件先识别，避免当前 GPU/CUDNN 运行时影响 CPU 小图 predictor；随后逐颗读取存储芯片。
    drams = recognize_dynamic_drams(
        image_path, side, current_year=current_year, template_id=template_id)
    by_slot = {item["slot"]: item for item in loc["slots"]}
    for dc in drams:
        slot = by_slot.get(dc.slot + 1)
        if slot is not None and dc.box:
            slot["particles"].append({"box": dc.box, "status": dc.status,
                                      "raw": dc.raw, "idx": dc.idx})
    results = drams + components

    # 低置信项标记人工复核后，再检查读数是否其实来自相邻框。
    for dc in results:
        if dc.code_type == "dram":
            continue                          # 颗粒之间日期本就相同，靠 roi 排序防串，不适用此判据
        other = _clash(dc, results)
        if other is None:
            continue
        # 作废本框读数、转人工。不去猜"哪个才是对的"—— 猜错就是把别处的日期
        # 安到这处头上，正是铁律要防的。宁可标盲点让人看一眼。
        dc.year = dc.week = 0
        dc.week_start = ""
        dc.status = "raw"
        dc.note = (f"读数 {dc.raw} 与同槽 {other.code_type.upper()} 相同且两框纵向重叠，"
                   f"疑似读到了对方那一行，已作废，请人工确认")
    return results
