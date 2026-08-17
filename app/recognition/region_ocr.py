"""按型号模板的固定框识别内存条存储颗粒(DRAM)日期码。

流程：
  1. 按 template_id 取该型号模板（template_store），拿到该面归一化框坐标。
  2. 逐个 dram 框裁剪局部小图（带余量并放大）送 OCR，区域内识别更准。
  3. 输出原生识别结果，**不做任何多数校正/预测/填充**：
       status="ok"  = 干净 3 位解码出 YWW（误读 531 也照实给 25年31周）；
       status="raw" = 未能解码，raw 字段存 OCR 原始读数，原样展示。

PCB / 主控为单独特写照片，整图识别（recognize_chip），不走模板。
"""
from __future__ import annotations

import datetime
import os
import re
from typing import Optional

import numpy as np
from PIL import Image

from .ocr_engine import get_engine, _predict_array, recognize
from . import template_store
from .date_parser import (
    DateCode, parse_detections, _week_start_date, _decode_yww, _decode_yyww,
    _RE_SEC_DATE, _RE_SERIAL_PREFIX, _RE_PURE4, _RE_PURE3,
)


def _denorm(box, W, H):
    return [[x * W, y * H] for x, y in box]


def _bbox(box):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


# OCR 置信度阈值：低于它的日期转人工复核（可用 OCR_CONF_MIN 覆盖）
_CONF_MIN = float(os.environ.get("OCR_CONF_MIN", "0.85"))

# 字符混淆挽救：激光点阵字里数字常被读成形近字母（5→S、0→O、1→I/L…）。
# 仅在 SEC/SAMSUNG 锚点后的 3 位后缀里做映射，避免误伤正常文本。
_CONF_MAP = str.maketrans({
    "O": "0", "o": "0", "Q": "0", "D": "0",
    "I": "1", "i": "1", "l": "1", "L": "1",
    "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "G": "6", "T": "7",
})
_RE_SEC_LOOSE = re.compile(r"(?:SEC|SAMSUNG)\s*([0-9OoQDIiLlSsBZzGT]{3})", re.IGNORECASE)


def _crop_region(img: Image.Image, box_px, pad_ratio: float = 0.6, min_h: int = 96):
    """裁出框+余量的局部图（小图放大保住小字）。返回 PIL 图或 None。

    只要图。需要把局部图里的坐标映射回原图时用 `_crop_region_geo`。
    """
    got = _crop_region_geo(img, box_px, pad_ratio, min_h)
    return got[0] if got else None


def _crop_region_geo(img: Image.Image, box_px, pad_ratio: float = 0.6, min_h: int = 96):
    """同 `_crop_region`，但**连坐标变换一起返回**：(crop, ox, oy, scale) 或 None。

    局部图里的点 (px, py) 映射回原图 = (ox + px / scale, oy + py / scale)。

    为什么要它：原先把标注框收紧到只圈数字是**重新裁原框再跑一次 OCR**
    （_tight_digit_box），实测这一次占单框耗时 48% —— 而日期此时已经读出来了，
    这次纯粹是为了知道那几位数字在框内的像素位置。有了变换，就能直接复用
    读日期那次 OCR 已经拿到的 detection 框，省掉整整一次推理。
    """
    x0, y0, x1, y1 = _bbox(box_px)
    bw, bh = x1 - x0, y1 - y0
    px, py = bw * pad_ratio + 6, bh * pad_ratio + 6
    W, H = img.size
    cx0, cy0 = max(0, int(x0 - px)), max(0, int(y0 - py))
    cx1, cy1 = min(W, int(x1 + px)), min(H, int(y1 + py))
    if cx1 <= cx0 or cy1 <= cy0:
        return None
    crop = img.crop((cx0, cy0, cx1, cy1))
    scale = 1.0
    if 0 < crop.height < min_h:
        f = min_h / crop.height
        crop = crop.resize((max(1, int(crop.width * f)), int(crop.height * f)))
        scale = f
    return crop, cx0, cy0, scale


def _roi_local(box_px, ox: float, oy: float, scale: float, pad: float = 0.30):
    """原框在**局部裁图坐标**里的矩形 (x0,y0,x1,y1)，四周放宽 pad 比例。

    为什么要它：裁图带 0.6 的 padding（det 需要留白才稳），于是相邻颗粒的丝印
    也会进画面。颗粒之间日期完全相同，`text.find(digits)` 会命中邻居那一行 ——
    实测某框标注框因此画到了上一颗身上（偏 236px）。更要紧的是**读数**也可能
    取自邻居：那等于把旁边那颗的日期安到本颗头上，被偷换的正好是这颗就漏判了。
    放宽 30% 是给模板滑框留余量（模板框本身只有颗粒宽度的 37%）。
    """
    x0, y0, x1, y1 = _bbox(box_px)
    s = scale or 1.0
    lx, ly = (x0 - ox) * s, (y0 - oy) * s
    rx, ry = (x1 - ox) * s, (y1 - oy) * s
    px, py = (rx - lx) * pad, (ry - ly) * pad
    return (lx - px, ly - py, rx + px, ry + py)


def _order_dets(dets: list[tuple], roi) -> list[tuple]:
    """把 detection 按"离原框有多近"重排：先交叠的，再按中心距由近到远。

    为什么不直接**丢掉**框外的：模板框会滑（实测某槽滑动量达框高的 65%），
    硬过滤时滑出去的框会一颗都读不到，读出率倒退。排序只改优先级 ——
    本颗那一行排在最前，邻居那一行仍作为最后的兜底候选，读出率不降。
    """
    if not roi or not dets:
        return dets
    bx0, by0, bx1, by1 = roi
    cx, cy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0

    def key(d):
        box = d[2]
        if not box:
            return (1, 1, 0.0)                     # 没框的排最后，无从判断远近
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        ax0, ay0, ax1, ay1 = min(xs), min(ys), max(xs), max(ys)
        hit = ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1
        dx = (ax0 + ax1) / 2.0 - cx
        dy = (ay0 + ay1) / 2.0 - cy
        # 纵向权重更大：颗粒是上下排布，串行（读到上/下一颗）比串列更容易发生
        return (0 if hit else 1, 0, abs(dy) * 3.0 + abs(dx))

    return sorted(dets, key=key)


def _ocr_dets(engine, crop) -> list[tuple]:
    """对局部图 OCR，返回 [(text, score, box)]（box 是局部图坐标，可能为 None）。

    第三项 box 是为"标注框收紧"服务的：读日期时顺手把 detection 的框带出来，
    就不必事后再跑一次 OCR 去定位那几位数字（见 `_crop_region_geo` 注释）。
    """
    if crop is None:
        return []
    dets = _predict_array(engine, np.asarray(crop.convert("RGB")))
    return [(d["text"], float(d.get("score", 0.0)), d.get("box"))
            for d in dets if d.get("text")]


def _read_dram(dets: list[tuple], year: int):
    """原生识别：从 [(text,score,box)] 里取日期读数。返回 (year, week, raw, score, hit)。

    不做多数校正——能解码为合法 YWW 就给年/周（误读 531 也照实给）；否则 year/week=None。
    score 为选中读数所在 OCR 检测的置信度（供"低置信→大模型"判断）。
    hit = (整行文本, 局部图框)，供标注框收紧复用，免得再跑一次 OCR。
    """
    best = None                                   # (raw, score, text, box)
    for t, s, bx in dets:                         # 1) SEC/SAMSUNG 后的数字
        m = _RE_SEC_DATE.search(t)
        if m:
            best = (m.group(1), s, t, bx)
            break
    if not best:                                  # 1b) 字符混淆挽救：SEC 后 3 个"数字或易混字母"→映射回数字
        for t, s, bx in dets:                     #     如 SECS40→540、SECB4O→840… 专治 5→S/0→O 等误读
            m = _RE_SEC_LOOSE.search(t)
            if m:
                mapped = m.group(1).translate(_CONF_MAP)
                if re.fullmatch(r"\d{3}", mapped):
                    best = (mapped, s, t, bx)
                    break
    if not best:                                  # 2) 任意独立 3 位
        for t, s, bx in dets:
            for tok in re.split(r"[\s/]+", t):
                if re.fullmatch(r"\d{3}", tok):
                    best = (tok, s, t, bx)
                    break
            if best:
                break
    if not best:                                  # 3) 任意 2~4 位连续数字
        for t, s, bx in dets:
            mm = re.search(r"\d{2,4}", t)
            if mm:
                best = (mm.group(0), s, t, bx)
                break
    if not best:                                  # 4) 数字最多的 token
        cand = None
        for t, s, bx in dets:
            for tok in re.split(r"[\s/]+", t):
                if any(c.isdigit() for c in tok) and (
                        cand is None or sum(c.isdigit() for c in tok) > sum(c.isdigit() for c in cand[0])):
                    cand = (tok, s, t, bx)
        best = cand
    if not best:
        return None, None, "", 0.0, None
    raw, score, text, bx = best
    hit = (text, bx)
    if re.fullmatch(r"\d{3}", raw):
        d = _decode_yww(raw, year)
        if d:
            return d[0], d[1], raw, score, hit
    return None, None, raw, score, hit


def _read_yyww(dets: list[tuple], year: int):
    """从 [(text,score)] 里取 **4 位 YYWW**（PCB/主控框用）。返回 (year, week, raw, score)。

    丝印常带前缀(如 K7**2536**)，连着就成 "72536" 5 位——不能傻取前 4 位(会得 7253→1972)。
    做法：对每段数字滑窗取所有 4 位候选，解码后**优先能对上近年**(cur-12..cur+1)的那个。
    """
    cur = year or datetime.date.today().year
    cands = []                                     # (score, y, w, raw, recent, text, box)
    for t, s, bx in dets:
        for run in re.findall(r"\d+", t):
            for i in range(0, max(1, len(run) - 3)):
                w4 = run[i:i + 4]
                if len(w4) != 4:
                    continue
                d = _decode_yyww(w4, cur)
                if d:
                    recent = 1 if (cur - 12) <= d[0] <= (cur + 1) else 0
                    cands.append((s, d[0], d[1], w4, recent, t, bx))
    if not cands:
        return None, None, "", 0.0, None
    # 优先近年，其次 OCR 置信，其次年份更新
    best = max(cands, key=lambda c: (c[4], c[0], c[1]))
    return best[1], best[2], best[3], best[0], (best[5], best[6])


def _tight_box_from_hit(hit, digits: str, ox: float, oy: float, scale: float):
    """从**读日期那次**已经拿到的 detection 算出"只圈那几位数字"的框（不跑 OCR）。

    hit = (整行文本, 局部图框)，来自 `_eval_crop`；ox/oy/scale 来自 `_crop_region_geo`。
    原先这一步是在原框内**重新跑一次 OCR**（_tight_digit_box），实测占单框耗时 48%，
    而日期此时早已读出 —— 纯粹为了定位数字的像素位置。改成复用后这次推理彻底省掉。

    切分逻辑与原来一致：按字符近似等宽，取数字子串在整行里的横向占比。
    拿不到就返回 None（调用方保留原框，绝不出错）。
    """
    if not digits or not hit:
        return None
    text, box = hit
    if not box or not text:
        return None
    idx = text.find(digits)                        # 该行文字里数字串的位置
    if idx < 0:
        return None
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    lx, rx, ty, by = min(xs), max(xs), min(ys), max(ys)
    n = max(1, len(text))
    fx0 = lx + (idx / n) * (rx - lx)
    fx1 = lx + ((idx + len(digits)) / n) * (rx - lx)
    m = 0.06 * (by - ty)                           # 上下留一丢丢余量
    s = scale or 1.0
    # 局部图坐标 → 原图坐标（先除放大倍数，再加裁图原点）
    return [[ox + fx0 / s, oy + (ty - m) / s], [ox + fx1 / s, oy + (ty - m) / s],
            [ox + fx1 / s, oy + (by + m) / s], [ox + fx0 / s, oy + (by + m) / s]]


def _enhance(crop: Image.Image) -> Image.Image:
    """局部对比增强(CLAHE)：救回低对比/发灰的激光打标小字。"""
    try:
        import cv2
        arr = np.asarray(crop.convert("L"))
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        out = clahe.apply(arr)
        return Image.fromarray(out).convert("RGB")
    except Exception:
        return crop


def _eval_crop(engine, im: Image.Image, year: int, reader=_read_dram, roi=None):
    """对一张(预处理后的)图 OCR 并取日期读数。
    返回 (y, w, raw, score, joined, text_score, hit)。

    reader：颗粒用 `_read_dram`(3位YWW)，PCB/主控用 `_read_yyww`(4位YYWW)。
    hit = (整行文本, 局部图框)：选中读数所在的那条 detection，供标注框收紧复用。
    roi：原框在局部图里的位置（`_roi_local`）。给了就按离原框远近排序 detection，
         让本颗那一行优先被取到，避免读成 padding 里带进来的邻居颗粒。
    """
    dets = _ocr_dets(engine, im)
    joined = " ".join(t for t, _, _ in dets)
    y, w, raw, score, hit = reader(_order_dets(dets, roi), year)
    text_score = max((s for _, s, _ in dets), default=0.0)  # 该图"有没有文字"的强度
    return y, w, raw, score, joined, text_score, hit


def _best_read(engine, crop: Image.Image, year: int, reader=_read_dram, try_rot180=False,
               roi=None, try_rot90=False):
    """读一个框：**只读增强图一次**。返回 (y, w, raw, score, joined, variant, best_img, hit)。

    为什么只读一次（本次改动）：
      原先每框最坏跑 5 次 OCR —— 增强读、原图重试、PCB 还试翻转与翻转+增强，
      读出后 _tight_digit_box 又整框重跑一次收紧标注框，低置信时 _prep_for_vl
      再跑两次判朝向。而实测单次推理里真正在"读日期"的只有前一两次：
      收紧标注框那一次占单框耗时 48%，判朝向那两次也不影响读数。

      代价（已知并接受）：按 CLAUDE.md 记录，只靠增强那一次是反面 82/84，
      加原图兜底才 84/84 —— 所以约 2 颗会从"OCR 读出"退成"OCR 未读出"。
      这 2 颗会落到已有的大模型逐颗兜底（_vl_fallback_dram），**不会变成盲点**，
      业务铁律不破。用一次推理换 2 颗走兜底是划算的。

      try_rot180 保留形参是为了不动调用方签名，但**不再试翻转**：
      PCB 倒印交给大模型兜底处理 —— 其提示词已写明"可能 180° 倒印、
      若正着看不成字请当作倒字来读"，本会话实测它认得倒字。

    hit = (整行文本, 局部图框)：选中读数所在的 detection，供标注框收紧复用。
    """
    if crop is None:
        return None, None, "", 0.0, "", "orig", None, None
    # 对比增强：激光小字/丝印对比度低，增强后命中率明显更高
    # （CLAUDE.md 实测反面 7.3s→4.7s、读出 72→82）。
    variants = [("enh", _enhance(crop), roi)]
    if try_rot90:
        variants.extend([
            ("rot90cw", _enhance(crop.transpose(Image.Transpose.ROTATE_270)), None),
            ("rot90ccw", _enhance(crop.transpose(Image.Transpose.ROTATE_90)), None),
        ])
    reads = []
    for name, image, local_roi in variants:
        y, w, raw, score, joined, text_score, hit = _eval_crop(
            engine, image, year, reader, local_roi)
        reads.append((bool(y), score, text_score, y, w, raw, joined, name, image, hit))
        if y and score >= _CONF_MIN:
            break
    best = max(reads, key=lambda row: (row[0], row[1], row[2]))
    _, score, _, y, w, raw, joined, name, image, hit = best
    return y, w, raw, score, joined, name, image, hit


# --------- 托盘槽位占位检测（空盘/不满盘：先数数量，只识别有条的槽）---------

# 边缘像素占比阈值：空槽(光滑塑料)边缘极少→接近 0；有条(密集芯片+激光小字)→远高于此。
# 真机用满盘/空盘各拍一张跑 tools/calibrate_slots.py 标定后，用 SLOT_PRESENCE_MIN 覆盖。
_SLOT_PRESENCE_MIN = float(os.environ.get("SLOT_PRESENCE_MIN", "0.035"))


def _box_center_x(box) -> float:
    xs = [p[0] for p in box]
    return sum(xs) / len(xs)


def _auto_slot_rects(boxes) -> list:
    """无显式 slots 时的兜底：按各框 **x 跨度并集** 切分成若干槽（左→右外接矩形）。

    一根内存条即使颗粒分成多列，各列 x 跨度相接/相近 → 并成一段；根与根之间有明显空 x
    带 → 断开。合并间隙取 `max(0.05, 6×中位框宽)`。**仅供无 slots 的旧模板临时用**；
    双列/紧排布局易误切，真机 4 槽模板请在模板里写显式 `slots`（见 recognize_side）。
    """
    spans = []                                    # (xmin, xmax, box)
    widths = []
    for b in boxes:
        box = b.get("box")
        if not box:
            continue
        xs = [p[0] for p in box]
        spans.append((min(xs), max(xs), box))
        widths.append(max(xs) - min(xs))
    if not spans:
        return []
    spans.sort(key=lambda s: s[0])
    med_w = sorted(widths)[len(widths) // 2]
    merge_gap = max(0.05, med_w * 6.0)            # 同根相邻列可跨此间隙合并；根间空带远大于此
    groups = [[spans[0]]]
    cur_max = spans[0][1]
    for s in spans[1:]:
        if s[0] - cur_max <= merge_gap:
            groups[-1].append(s)
            cur_max = max(cur_max, s[1])
        else:
            groups.append([s])
            cur_max = s[1]
    rects = []
    for g in groups:
        allx = [p[0] for (_, _, box) in g for p in box]
        ally = [p[1] for (_, _, box) in g for p in box]
        rects.append([min(allx), min(ally), max(allx), max(ally)])
    rects.sort(key=lambda r: r[0])
    return rects


def _slot_axis(layout) -> str:
    return "vertical" if layout.get("slot_axis") == "vertical" else "horizontal"


def _physical_slot_order(layout, count: int | None = None) -> list[int]:
    """Map visual slot index to tray physical slot index."""
    if count is None:
        count = len(layout.get("slots") or [])
    identity = list(range(count))
    raw = layout.get("physical_slot_order")
    if not raw:
        return identity
    try:
        order = [int(value) for value in raw]
    except (TypeError, ValueError):
        return identity
    if len(order) != count:
        return identity
    if sorted(order) == identity:
        return order
    one_based = list(range(1, count + 1))
    if sorted(order) == one_based:
        return [value - 1 for value in order]
    return identity


def _physical_slot_for_visual(layout, visual_slot: int,
                              count: int | None = None) -> int:
    order = _physical_slot_order(layout, count)
    if 0 <= visual_slot < len(order):
        return order[visual_slot]
    return visual_slot


def _slot_rects_for_layout(layout) -> list:
    """取该面槽位矩形；横向左到右，纵向上到下。"""
    slots = layout.get("slots")
    if slots:
        axis = _slot_axis(layout)
        index = 1 if axis == "vertical" else 0
        return sorted(([float(v) for v in s] for s in slots), key=lambda r: r[index])
    return _auto_slot_rects(layout.get("boxes", []))


def _slot_of_box(box, slot_rects) -> int:
    """框中心落在哪个槽矩形内；越界时取二维中心最近的槽。"""
    if not slot_rects:
        return -1
    cx = _box_center_x(box)
    cy = sum(p[1] for p in box) / len(box)
    for i, (x0, y0, x1, y1) in enumerate(slot_rects):
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return i
    return min(range(len(slot_rects)), key=lambda i: (
        (cx - (slot_rects[i][0] + slot_rects[i][2]) / 2) ** 2 +
        (cy - (slot_rects[i][1] + slot_rects[i][3]) / 2) ** 2))


def detect_occupied_slots(img: Image.Image, slot_rects, thr: Optional[float] = None,
                          axis: str = "horizontal") -> list[dict]:
    """判每个托盘槽位有没有内存条（边缘/纹理密度）。返回 [{slot,occupied,score,box}]（左→右）。

    空槽=光滑塑料→边缘极少；有条=密集芯片+激光小字→边缘多。thr 缺省用 SLOT_PRESENCE_MIN。
    检测异常时保守判为"有条"（宁可多识别，绝不漏掉真条）。
    """
    if thr is None:
        thr = _SLOT_PRESENCE_MIN
    W, H = img.size
    out = []
    index = 1 if axis == "vertical" else 0
    for i, rect in enumerate(sorted(slot_rects, key=lambda r: r[index])):
        x0, y0, x1, y1 = rect
        cx0, cy0 = max(0, int(x0 * W)), max(0, int(y0 * H))
        cx1, cy1 = min(W, int(x1 * W)), min(H, int(y1 * H))
        score = 1.0
        if cx1 > cx0 and cy1 > cy0:
            try:
                import cv2
                arr = np.asarray(img.crop((cx0, cy0, cx1, cy1)).convert("L"))
                edges = cv2.Canny(arr, 60, 160)
                score = float((edges > 0).mean())
            except Exception:
                score = 1.0                       # 检测失败 → 保守当作有条
        out.append({"slot": i, "occupied": score >= thr,
                    "score": round(score, 4), "box": [cx0, cy0, cx1, cy1]})
    return out


# 白标签判据：又白又平（高亮度 + 低边缘）→ 这个框位压在标签上，别把标签上的字当日期读。
_LABEL_WHITE_MIN = float(os.environ.get("LABEL_WHITE_MIN", "205"))   # 平均亮度阈值(0~255)
_LABEL_EDGE_MAX = float(os.environ.get("LABEL_EDGE_MAX", "0.03"))    # 边缘占比上限
# **部分遮挡**判据：框内灰度标准差上限。白标签压住一部分、芯片露一部分时明暗混杂，std 飙升。
# 实测正面主控：未遮挡那槽 std=29.2（正确读出 2534）；被标签盖住日期行的三槽 std=94.7~107.8；
# 未遮挡的 PCB 框 std=31~44。取 70 两侧都留足余量。
# 必须先判再读：这种框硬读会把标签上的条码编号当成日期（实测读出 2202/1411/1108 假值），
# 而假日期会参与超差判定 —— 比"读不出"危险得多（读不出只是转人工，假值会导致误判）。
_OCCLUDE_STD_MAX = float(os.environ.get("OCCLUDE_STD_MAX", "70"))
_OCCLUDE_EDGE_MAX = float(os.environ.get("OCCLUDE_EDGE_MAX", "0.12"))


def _box_kind(img: Image.Image, box_px) -> str:
    """判一个模板框位落在什么上：'label'(白标签/大片白) / 'chip'(芯片，正常识别)。

    标签是**又白又平**（平均亮度高、边缘极少）；芯片是黑塑封 + 密集激光小字（边缘多）。
    检测异常时保守当 'chip'（宁可去识别，也不误跳过真芯片）。
    """
    try:
        import cv2
        x0, y0, x1, y1 = _bbox(box_px)
        W, H = img.size
        cx0, cy0 = max(0, int(x0)), max(0, int(y0))
        cx1, cy1 = min(W, int(x1)), min(H, int(y1))
        if cx1 <= cx0 or cy1 <= cy0:
            return "chip"
        gray = np.asarray(img.crop((cx0, cy0, cx1, cy1)).convert("L"))
        bright = float(gray.mean())
        edges = float((cv2.Canny(gray, 60, 160) > 0).mean())
        if bright >= _LABEL_WHITE_MIN and edges <= _LABEL_EDGE_MAX:
            return "label"                          # 整框压在纯白标签上
        if float(gray.std()) >= _OCCLUDE_STD_MAX and edges <= _OCCLUDE_EDGE_MAX:
            return "label"                          # 部分遮挡：明暗混杂，读了只会读到标签上的字
        return "chip"
    except Exception:
        return "chip"


def _assign_dram_idx(codes: list[DateCode]):
    """给存储颗粒按空间顺序（从上到下、从左到右）编号 idx=1..N，用于标注与定位。"""
    drams = [c for c in codes if c.code_type == "dram" and c.box]
    def key(c):
        xs = [p[0] for p in c.box]; ys = [p[1] for p in c.box]
        return (round(min(ys) / 40), min(xs))   # 先按行(40px 容差)再按列
    for i, c in enumerate(sorted(drams, key=key), 1):
        c.idx = i


def recognize_side(image_path: str, side: str,
                   current_year: Optional[int] = None,
                   template_id: Optional[str] = None,
                   occ_out: Optional[list] = None,
                   code_types: Optional[set[str]] = None) -> list[DateCode]:
    """按所选型号模板的固定框只识别存储颗粒(DRAM)，输出原生识别结果。

    - 能解码为合法年/周就给日期（不做多数校正、不预测，误读也照实给）。
    - 不能解码就把 OCR 的原始读数原样写上（status="raw"）；完全无读数才空。
    template_id 缺省用 template_store.default_template_id()。
    PCB / 主控由 recognize_chip 单独识别，不在此处。

    托盘空位：识别前先判每个槽有没有条（`detect_occupied_slots`），**空槽的框整槽跳过**、
    不识别不判定。占位概要（含空/满 + 分数）写入 `occ_out`（若提供）供上层与标注使用。
    """
    if current_year is None:
        current_year = datetime.date.today().year

    tid = template_id or template_store.default_template_id()
    tpl = template_store.get_template(tid) if tid else None
    if not tpl:
        raise ValueError(f"未找到模板：{template_id!r}，请先在模板库中选择/创建模板")
    layout = (tpl.get("sides") or {}).get(side)
    if not layout:
        raise ValueError(f"模板 {tid} 不含「{side}」面")

    img = Image.open(image_path).convert("RGB")
    W, H = img.size
    engine = get_engine()

    # 托盘空位：先判每个槽有没有条 → 空槽整槽跳过
    slot_rects = _slot_rects_for_layout(layout)
    occ = detect_occupied_slots(img, slot_rects, axis=_slot_axis(layout)) if slot_rects else []
    occupied = {o["slot"] for o in occ if o["occupied"]}
    if occ_out is not None:
        occ_out.clear()
        occ_out.extend(occ)

    results: list[DateCode] = []
    pending: list[tuple] = []                     # dram(3位)：低置信/未读，待人工复核
    pending4: list[tuple] = []                    # pcb/主控(4位)：低置信/未读，待人工复核
    for slot in sorted(layout["boxes"], key=lambda b: b.get("id", 0)):
        stype = slot.get("type")
        if stype not in ("dram", "pcb", "controller"):
            continue
        if code_types is not None and stype not in code_types:
            continue
        box_slot = _slot_of_box(slot["box"], slot_rects) if slot_rects else -1
        if occ and box_slot not in occupied:      # 该框所在槽是空位 → 跳过（不识别、不判定）
            continue
        box_px = _denorm(slot["box"], W, H)
        if _box_kind(img, box_px) == "label":     # 框位压在白标签上 → 整框跳过，别把标签数字当日期
            continue
        chip_box = stype == "dram" and layout.get("dram_box_mode") == "chip"
        geo = _crop_region_geo(img, box_px, pad_ratio=0.08 if chip_box else 0.6,
                               min_h=160 if chip_box else 96)
        crop, ox, oy, cscale = geo if geo else (None, 0, 0, 1.0)
        # 原框在局部图里的位置：裁图带 padding，邻颗日期完全相同，
        # 不给这个约束就可能读到/框到旁边那一颗（见 _roi_local 注释）。
        roi = _roi_local(box_px, ox, oy, cscale) if geo else None
        if stype == "dram":
            # 只读增强图一次（见 _best_read 注释）；hit 供标注框收紧复用，不再重跑 OCR
            y, w, raw, score, joined, variant, best_img, hit = _best_read(
                engine, crop, current_year, roi=roi,
                try_rot90=layout.get("dram_rotation") == "auto90")
            score = round(score, 3)
            if y:
                note = "" if variant == "orig" else f"方向/增强校正（{variant}）"
                dc = DateCode(raw=raw, code_type="dram", year=y, week=w,
                              week_start=_week_start_date(y, w), confidence=score,
                              source_text=joined, box=box_px, digit_format="YWW", status="ok",
                              note=note, ocr_raw=raw, ocr_confidence=score)
            else:
                dc = DateCode(raw=raw, code_type="dram", year=0, week=0, week_start="",
                              confidence=score, source_text=joined, box=box_px,
                              digit_format="YWW", status="raw",
                              note="OCR 未解码，请人工复核", ocr_raw=raw, ocr_confidence=score)
            dc.slot = box_slot
            # 标注框收紧到只圈数字：复用刚才那次 OCR 的 detection，不再重跑
            if y and not variant.startswith("rot90"):
                tb = _tight_box_from_hit(hit, raw, ox, oy, cscale)
                if tb:
                    dc.box = tb
            results.append(dc)
            if (not y) or (score < _CONF_MIN):
                pending.append((dc, crop))
        else:
            # PCB / 主控：框出 4 位 YYWW，低置信转人工复核。
            y, w, raw, score, joined, variant, best_img, hit = _best_read(
                engine, crop, current_year, reader=_read_yyww, roi=roi)
            score = round(score, 3)
            note = ("" if variant == "orig" else f"方向/增强校正（{variant}）") if y else "OCR 未解码，请人工复核"
            dc = DateCode(raw=raw, code_type=stype, year=y or 0, week=w or 0,
                          week_start=_week_start_date(y, w) if y else "",
                          confidence=score, source_text=joined, box=box_px,
                          digit_format="YYWW", status="ok" if y else "raw",
                          note=note, ocr_raw=raw, ocr_confidence=score)
            dc.slot = box_slot
            if y:                                 # 同上：复用 detection 收紧标注框
                tb = _tight_box_from_hit(hit, raw, ox, oy, cscale)
                if tb:
                    dc.box = tb
            results.append(dc)
            # 年份离谱(超近年窗口)说明可能读到旁边料号，转人工复核。
            implausible = bool(y) and not ((current_year - 12) <= y <= (current_year + 1))
            if (not y) or (score < _CONF_MIN) or implausible:
                pending4.append((dc, crop))

    _vl_fallback_dram(pending, current_year)
    _vl_fallback_yyww(pending4, current_year)
    _assign_dram_idx(results)
    return results


def _prep_for_vl(engine, crop: Image.Image) -> Image.Image:
    """发给多模态大模型【之前】的统一预处理：只做对比增强(CLAHE)。

    原先还用 OCR 判朝向：原样与翻转 180° 各跑一次比文字置信，取更"正"的那个。
    **已去掉**——一次判朝向就是两次推理，而大模型不像 OCR 的 det 那样依赖行方向：
    本会话实测它认得倒字（PCB 倒印槽位直接读出、SOT 三行倒排也读出 511），
    且 prompts.PCB_DATE 已写明"可能 180° 倒印，若正着看不成字请当作倒字来读"。
    engine 形参保留，不动调用方签名。
    """
    if crop is None:
        return None
    return _enhance(crop)


def _vl_fallback_dram(pending: list[tuple], year: int):
    """低置信或未读颗粒保留为人工复核项，不调用多模态模型。"""
    for dc, _ in pending:
        if not dc.ocr_raw:
            dc.ocr_raw, dc.ocr_confidence = dc.raw, dc.confidence
        oraw = dc.ocr_raw or "空"
        dc.note = f"OCR原文「{oraw}」(置信{dc.ocr_confidence})低或未解码，请人工复核"


def _vl_fallback_yyww(pending: list[tuple], year: int):
    """PCB/主控低置信或未读项保留为人工复核，不调用多模态模型。"""
    for dc, _ in pending:
        if not dc.ocr_raw:
            dc.ocr_raw, dc.ocr_confidence = dc.raw, dc.confidence
        oraw = dc.ocr_raw or "空"
        dc.note = f"OCR原文「{oraw}」(置信{dc.ocr_confidence})低或未解码，请人工复核"


def _controller_vl_fallback(image_path: str, codes: list, current_year: int):
    """兼容旧调用点；当前规则模式不再使用多模态主控兜底。"""
    return


def recognize_rules(image_path: str,
                    current_year: Optional[int] = None,
                    side: Optional[str] = None,
                    tile_bands: Optional[int] = None) -> list[DateCode]:
    """规则识别（不依赖固定坐标框）：整图 OCR → 按规则在全图自动找出日期码。

    PaddleOCR 扫全图（含分块），再由 `parse_detections` 按规则识别：
      - 颗粒 SEC ### (YWW)、主控序列号前缀 (YYWW)、PCB 纯 4 位 (YYWW)；
      - 用「token 形态 + 周数合法 + 邻近厂商关键字」三重约束抑制料号/规格误判。
    原生模式（correct=False）：不预测、不多数校正，读残行原样作为 raw 输出。
    """
    if current_year is None:
        current_year = datetime.date.today().year
    dets = recognize(image_path, tile_bands=tile_bands)
    codes = parse_detections(dets, current_year=current_year, correct=False)
    img = Image.open(image_path).convert("RGB")
    # 未解码的颗粒 → 裁其框区交大模型兜底（规则模式无逐颗真实分数，仅对未读的兜底）
    pend = [c for c in codes if c.code_type == "dram" and not c.week and c.box]
    if pend:
        _vl_fallback_dram([(c, _crop_region(img, c.box)) for c in pend], current_year)
    # 标注框收紧到只圈数字（颗粒）；主控/PCB 保持整行框。
    # 规则模式的 c.box 已是整图坐标下那条 detection 的框、c.source_text 是整行文本，
    # 所以直接套 _tight_box_from_hit（ox=oy=0、scale=1），不必像原先那样重跑一次 OCR。
    for c in codes:
        if c.code_type == "dram" and c.week and c.box:
            tb = _tight_box_from_hit((c.source_text or c.raw, c.box), c.raw, 0, 0, 1.0)
            if tb:
                c.box = tb
    _assign_dram_idx(codes)                         # 不再标遮挡：读不出就原样展示 raw
    if side == "front":                              # 正面主控(RCD)OCR读不到→大模型兜底
        try:
            _controller_vl_fallback(image_path, codes, current_year)
        except Exception:  # noqa: BLE001
            pass
    return codes


def _pick_chip_date(dets: list[dict], year: int):
    """从单芯片照片的整图 OCR 结果里挑出最可信的 YYWW 日期。

    返回 (raw, (year, week), box, source_text) 或 None。
    主控多为序列号前缀(2517A0DRCR)，PCB 多为独立 4 位(2530)；
    两者都按 YYWW 解码，序列号前缀略加权。
    """
    cands = []  # (score, raw, (y,w), box, text)
    for d in dets:
        text = (d.get("text") or "").strip()
        score = float(d.get("score", 0.0))
        box = d.get("box")
        m = _RE_SERIAL_PREFIX.match(text)
        if m:
            dec = _decode_yyww(m.group(1), year)
            if dec:
                cands.append((score + 0.2, m.group(1), dec, box, text))
        for tok in re.split(r"[\s/]+", text):
            if _RE_PURE4.match(tok):
                dec = _decode_yyww(tok, year)
                if dec:
                    cands.append((score, tok, dec, box, text))
    if not cands:
        return None
    cands.sort(key=lambda x: -x[0])
    _, raw, dec, box, text = cands[0]
    return raw, dec, box, text


def recognize_chip(image_path: str, kind: str,
                   current_year: Optional[int] = None) -> list[DateCode]:
    """识别单独上传的 PCB / 主控芯片特写照片，返回 [DateCode]（0~1 个）。

    kind: "pcb" 或 "controller"。整图 OCR（含分块）后挑最可信的 YYWW。
    """
    if current_year is None:
        current_year = datetime.date.today().year
    if kind not in ("pcb", "controller"):
        raise ValueError(f"未知芯片类型：{kind}")

    dets = recognize(image_path)
    picked = _pick_chip_date(dets, current_year)
    if picked:
        raw, (y, w), box, text = picked
        return [DateCode(
            raw=raw, code_type=kind, year=y, week=w,
            week_start=_week_start_date(y, w), confidence=0.85,
            source_text=text, box=box, digit_format="YYWW")]
    return [DateCode(
        raw="", code_type=kind, year=0, week=0, week_start="",
        confidence=0.0, source_text="", box=None, status="unknown",
        note="未能识别，请人工确认或重拍清晰特写")]


def recognize_pcb(image_path: str,
                  current_year: Optional[int] = None) -> list[DateCode]:
    """仅用本地 PaddleOCR 读取 PCB 日期，未读出则转人工复核。"""
    if current_year is None:
        current_year = datetime.date.today().year

    picked = _pick_chip_date(recognize(image_path), current_year)
    if picked:
        raw, (year, week), box, source = picked
        return [DateCode(
            raw=raw, code_type="pcb", year=year, week=week,
            week_start=_week_start_date(year, week), confidence=0.85,
            source_text=source, box=box, digit_format="YYWW", status="ok")]

    return [DateCode(
        raw="", code_type="pcb", year=0, week=0, week_start="",
        confidence=0.0, source_text="", box=None, status="unknown",
        note="本地 OCR 未读出，请重拍更清晰的丝印特写或人工复核")]
