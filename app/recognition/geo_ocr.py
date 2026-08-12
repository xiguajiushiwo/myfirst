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
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger("yxq.geo")
from .date_parser import (DateCode, _decode_yww, _decode_yyww, _week_start_date)
from .ocr_engine import get_component_engine
from .region_ocr import (_CONF_MIN, _box_kind, _bbox, _crop_region_geo, _enhance,
                         _eval_crop, _read_dram, _read_yyww, _roi_local,
                         _tight_box_from_hit, _vl_fallback_yyww)
from .region_ocr import recognize_side

# PCB 倒读也可能解出**结构合法**的假值：实测槽1 正读 2536(置信1.00)、
# 倒读 2025(置信0.21)，而 2025 也能通过年/周合法性检查。所以只靠"解出来了"
# 不足以定方向，得看置信差。两向置信都高且接近时**不猜** —— 标盲点转人工。
_ROT_AMBIG_RATIO = float(os.environ.get("GEO_ROT_AMBIG_RATIO", "2.0"))
_ROT_STRONG_SCORE = float(os.environ.get("GEO_ROT_STRONG_SCORE", "0.98"))
_ROT_WEAK_SCORE_MAX = float(os.environ.get("GEO_ROT_WEAK_SCORE_MAX", "0.90"))


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


def _read_pcb_box(engine, crop, year):
    """PCB：不增强、2× 放大、试正/倒两向，按置信取。返回值末位是 note。

    不传 roi：PCB 框已经收窄到日期码本身（条宽的 12%、高 49px），
    框内没有邻居可混，而翻转后 roi 坐标要跟着折算，徒增出错面。

    两向都解出且置信接近 → 不猜，note 里标明需人工确认（调用方据此转盲点）。
    """
    base = _scaled(crop, 2)
    got = {}
    for rot in (0, 180):
        im = base.rotate(180) if rot else base
        y, w, raw, score, joined, _ts, hit = _eval_crop(engine, im, year, _read_yyww, None)
        got[rot] = (y, w, raw, score, joined, hit)

    valid = [(rot, v) for rot, v in got.items() if v[0]]
    if not valid:
        gray = _scaled(crop.convert("L").convert("RGB"), 2)
        for rot in (0, 180):
            im = gray.rotate(180) if rot else gray
            y, w, raw, score, joined, _ts, hit = _eval_crop(
                engine, im, year, _read_yyww, None)
            got[rot] = (y, w, raw, score, joined, hit)
        valid = [(rot, v) for rot, v in got.items() if v[0]]
    if not valid:
        y, w, raw, score, joined, hit = got[0]
        return y, w, raw, score, joined, hit, ""
    if len(valid) == 1:
        rot, (y, w, raw, score, joined, hit) = valid[0]
        # 倒印时 hit 的框在翻转后的图里，折算回来太绕且易错 —— 直接不收紧标注框
        return y, w, raw, score, joined, (hit if rot == 0 else None), \
            ("倒印180°" if rot else "")
    # 两向都解出：取置信高者，并判是否构成歧义
    (r_hi, hi), (r_lo, lo) = sorted(valid, key=lambda kv: -kv[1][3])
    y, w, raw, score, joined, hit = hi
    lo_raw, lo_score = lo[2], lo[3]
    strong_winner = score >= _ROT_STRONG_SCORE and lo_score <= _ROT_WEAK_SCORE_MAX
    ambig = lo_score > 0 and score < lo_score * _ROT_AMBIG_RATIO and not strong_winner
    note = (f"正/倒两向都解出且置信接近（{raw}@{score:.2f} vs {lo_raw}@{lo_score:.2f}），"
            f"无法定向，请人工确认"
            if ambig else
            f"两向都解出，取置信高者 {raw}@{score:.2f}（另一向 {lo_raw}@{lo_score:.2f}）")
    return (None if ambig else y), (None if ambig else w), raw, score, joined, \
        (hit if r_hi == 0 else None), note


def _read_controller_box(engine, crop, year):
    """主控/RCD 与 PCB 都使用四位 YYWW，并同时尝试正向和 180 度。"""
    return _read_pcb_box(engine, crop, year)


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
        for b in layout.get("boxes") or []:
            kind = b.get("type")
            if kind not in ("pcb", "controller", "pmic", "sot") or not b.get("manual"):
                continue
            xs = [p[0] for p in b["box"]]
            ys = [p[1] for p in b["box"]]
            slot1 = int(b.get("slot", 0)) + 1        # 模板 slot 从 0 起，locate 用 1 起
            out.setdefault(slot1, {})[kind] = [min(xs), min(ys), max(xs), max(ys)]
        return out
    except Exception as e:  # noqa: BLE001
        log.info("读模板人工框失败(回退现算)：%s", e)
        return {}


def recognize_geo(image_path: str, current_year: Optional[int] = None,
                  loc_out: Optional[dict] = None,
                  side: str = "back", template_id: Optional[str] = None) -> list[DateCode]:
    """几何定位 + 逐框识别一整盘。返回 [DateCode]，`slot` 已按左→右填好（0 起）。

    loc_out：给了就把 `geo_locate.locate` 的原始定位结果写进去（供上层标注/诊断）。

    盲点口径与铁律一致：读不出的框照实留 status="raw"/年周为 0，
    由 `services.compute_signal` 挑出来转人工 —— **不猜、不填、不表决**。
    """
    if current_year is None:
        current_year = datetime.date.today().year

    img = Image.open(image_path).convert("RGB")
    # PCB/主控直接使用模板里四槽人工标定框，不再依赖动态切条结果。
    # 大标签会让正面动态切条只找到一根，从而把另外三根主控静默漏掉；固定框则能明确标出遮挡。
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

            if kind == "pcb":
                y, w, raw, score, joined, hit, note = _read_pcb_box(
                    engine, crop, current_year)
            else:
                y, w, raw, score, joined, hit, note = _read_controller_box(
                    engine, crop, current_year)
            score = round(score, 3)
            dc = _mk(kind, raw, y, w, score, joined, box_px,
                     note if y else (note or "OCR 未解码，请人工复核"),
                     "ok" if y else "raw")
            dc.slot = slot_i
            if y and hit:
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
    drams = recognize_side(
        image_path, side, current_year=current_year, template_id=template_id,
        code_types={"dram"})
    for slot_i in sorted({c.slot for c in drams if c.slot >= 0}):
        rows = [c for c in drams if c.slot == slot_i]
        rows.sort(key=lambda c: (
            min((p[1] for p in (c.box or [])), default=0),
            min((p[0] for p in (c.box or [])), default=0)))
        for idx, dc in enumerate(rows, 1):
            dc.idx = idx
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
