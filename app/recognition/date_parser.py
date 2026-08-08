"""内存条日期码解析与分类。

本模块不依赖 PaddleOCR，纯逻辑，便于单独测试。

支持三类日期码：
  - PCB 板上日期码 (pcb)        : 4 位 YYWW，例如 "2530" -> 2025 年第 30 周
  - 存储颗粒日期码 (dram)       : 3 位 YWW，例如 "534"  -> 2025 年第 34 周（首位是年份个位）
  - 主控/RCD 芯片日期码 (controller): 4 位 YYWW，常作为序列号前缀，例如 "2517A0DRCR" -> 2025 年第 17 周

设计要点：
  - OCR 结果里充斥着料号(K4RAH04)、规格(PC5-5600B-RA0-1010-XT)等噪声，
    若无脑提取数字会产生大量误判。因此采用「严格 token 形态 + 周数合法性 + 上下文关键字」三重约束。
  - 每个候选都带 confidence 与 source_text，前端会原样展示，便于人工复核。
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field, asdict
from typing import Optional


# ----------------------------- 配置 -----------------------------

# 主控 / RCD / SPD 芯片常见厂商或料号关键字（小写匹配）
CONTROLLER_KEYWORDS = {
    "rambus", "ddr5rcd", "ddr4rcd", "rcd", "spd", "pmic",
    "montage", "renesas", "idt", "rcd02", "rcd2",
}

# 存储颗粒(DRAM) 常见厂商关键字（小写匹配）
DRAM_KEYWORDS = {
    "sec", "samsung", "k4", "skhynix", "hynix", "micron", "mt", "nanya", "nt5",
}

TYPE_LABELS = {
    "pcb": "PCB 板上日期码",
    "dram": "存储颗粒日期码",
    "controller": "主控/RCD 芯片日期码",
    # 几何定位模式（mode="geo"）新增的两处：PMIC 电源管理芯片、SOT 小封装 8 脚器件
    "pmic": "PMIC 电源芯片日期码",
    "sot": "SOT 小封装器件日期码",
    "unknown": "未分类日期码",
}


# ----------------------------- 数据结构 -----------------------------

@dataclass
class DateCode:
    """一个被识别出的日期码候选。"""
    raw: str                     # 命中的原始数字串，如 "2530" / "534"
    code_type: str               # pcb / dram / controller / unknown
    year: int                    # 解码出的公历年份
    week: int                    # ISO 周数 (1-53)
    week_start: str              # 该周周一的日期 (YYYY-MM-DD)，近似
    confidence: float            # 0~1，分类把握度
    source_text: str             # 命中所在的完整 OCR 文本
    box: Optional[list] = None   # 文本框多边形 [[x,y],...]
    digit_format: str = ""       # "YYWW" 或 "YWW"
    note: str = ""               # 备注（如"疑似 OCR 误读，与多数不一致"）
    status: str = "ok"           # ok=已解码 / covered=看不到日期(遮挡) / raw=未解码 / unknown=无读数
    model_confidence: Optional[float] = None  # 多模态大模型自评置信度(0~1)，仅 PCB 双路识别用
    idx: Optional[int] = None    # 存储颗粒在该面的序号(1..N，从上到下从左到右)，用于定位
    slot: int = -1               # 所属托盘槽位(0..N-1，左→右)；-1=未分槽(规则模式/单根)
    ocr_raw: str = ""            # 送大模型【之前】PaddleOCR 的原始读数（透明展示，不被覆盖）
    ocr_confidence: float = 0.0  # PaddleOCR 原始真实置信度（不被大模型改写抹平）

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.code_type, self.code_type)

    @property
    def description(self) -> str:
        return f"{self.year} 年第 {self.week} 周"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type_label"] = self.type_label
        d["description"] = self.description
        return d


# ----------------------------- 工具函数 -----------------------------

def to_yyyyww(year: int, week: int) -> str:
    """(2025, 17) -> "202517"；无效返回空串。"""
    if year and week and 1 <= week <= 53:
        return f"{year:04d}{week:02d}"
    return ""


def _week_start_date(year: int, week: int) -> str:
    """返回该 ISO 周周一的日期字符串；非法则返回空串。"""
    try:
        return datetime.date.fromisocalendar(year, week, 1).isoformat()
    except ValueError:
        return ""


def _valid_week(week: int) -> bool:
    return 1 <= week <= 53


def _decode_yyww(digits: str, current_year: int) -> Optional[tuple[int, int]]:
    """解码 4 位 YYWW。返回 (year, week)；非法返回 None。"""
    yy = int(digits[:2])
    ww = int(digits[2:])
    if not _valid_week(ww):
        return None
    # 21 世纪优先：00-69 视为 2000+，70-99 视为 1900+
    year = 2000 + yy if yy <= 69 else 1900 + yy
    # 年份不应明显超过当前年份（容忍 +1）
    if year > current_year + 1:
        return None
    return year, ww


def _decode_yww(digits: str, current_year: int) -> Optional[tuple[int, int]]:
    """解码 3 位 YWW（首位为年份个位）。返回 (year, week)；非法返回 None。"""
    y = int(digits[0])
    ww = int(digits[1:])
    if not _valid_week(ww):
        return None
    # 在各个十年里，挑选个位为 y 且 <= 当前年份+1 的最近年份
    for base in range(2030, 1989, -10):
        cand = base + y
        if cand <= current_year + 1:
            return cand, ww
    return None


def _center(box) -> Optional[tuple[float, float]]:
    if not box:
        return None
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _dist(a, b) -> float:
    if a is None or b is None:
        return float("inf")
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


# ----------------------------- 主解析逻辑 -----------------------------

# 主控序列号前缀：4 位数字后紧跟字母（如 2517A0DRCR）
_RE_SERIAL_PREFIX = re.compile(r"^(\d{4})(?=[A-Za-z])")
# 整段就是 4 位数字
_RE_PURE4 = re.compile(r"^\d{4}$")
# 整段就是 3 位数字
_RE_PURE3 = re.compile(r"^\d{3}$")
# 形如 SEC 534 / SEC534；宽松到 2~3 位，残缺(2位)的留待多数表决补全
_RE_SEC_DATE = re.compile(r"(?:SEC|SAMSUNG)\s*(\d{2,3})", re.IGNORECASE)


def _context_string(idx: int, detections: list[dict], radius: float) -> str:
    """收集自身 + 空间邻近文本，拼成小写上下文串，用于关键字匹配。"""
    own = detections[idx]
    own_c = _center(own.get("box"))
    parts = [own.get("text", "")]
    for j, det in enumerate(detections):
        if j == idx:
            continue
        if _dist(own_c, _center(det.get("box"))) <= radius:
            parts.append(det.get("text", ""))
    return " ".join(parts).lower()


def _has_keyword(ctx: str, keywords: set[str]) -> bool:
    return any(k in ctx for k in keywords)


# 厂商日期行的前缀（三星颗粒第二行 "SEC ###"）
_VENDOR_ANCHORS = {"sec", "samsung"}


def _has_adjacent_vendor(idx: int, detections: list[dict], radius: float) -> bool:
    """裸 3 位码只有在紧邻 'SEC'/'SAMSUNG' 锚点时才可信（同一行很近）。

    满板都是 SEC/K4 料号时，'附近有关键字'会失效；这里要求锚点中心
    距离很近(< radius*0.5) 且大致同一水平行，避免把碎片误判为颗粒日期。
    """
    own_c = _center(detections[idx].get("box"))
    if own_c is None:
        return False
    own_h = 0.0
    box = detections[idx].get("box")
    if box:
        ys = [p[1] for p in box]
        own_h = max(ys) - min(ys)
    for j, det in enumerate(detections):
        if j == idx:
            continue
        t = (det.get("text") or "").strip().lower()
        if t not in _VENDOR_ANCHORS:
            continue
        c = _center(det.get("box"))
        if c is None:
            continue
        if _dist(own_c, c) <= radius * 0.5 and abs(own_c[1] - c[1]) <= max(own_h, 18) * 1.2:
            return True
    return False


def parse_detections(
    detections: list[dict],
    current_year: Optional[int] = None,
    correct: bool = True,
) -> list[DateCode]:
    """从 OCR 检测结果中提取并分类日期码（规则识别，不依赖固定坐标框）。

    参数 detections: [{"text": str, "score": float, "box": [[x,y]*4]}...]
    参数 correct: True=对读残行做多数补全、对离群读数降权（默认，旧行为）；
                  False=**原生模式**，不预测/不校正——读残行原样作为 status="raw" 输出。
    返回: DateCode 列表（已按类型/置信度排序）。
    """
    if current_year is None:
        current_year = datetime.date.today().year

    # 邻近半径：按所有框的中心间距估算一个尺度
    centers = [c for c in (_center(d.get("box")) for d in detections) if c]
    if len(centers) >= 2:
        xs = [c[0] for c in centers]
        ys = [c[1] for c in centers]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        radius = max(80.0, span * 0.12)
    else:
        radius = 200.0

    results: list[DateCode] = []
    pending: list[DateCode] = []  # 厂商日期行但数字被读残(只剩2位)，待多数表决补全

    for idx, det in enumerate(detections):
        text = (det.get("text") or "").strip()
        if not text:
            continue
        box = det.get("box")
        ctx = _context_string(idx, detections, radius)

        # 该 detection 内的所有「词」
        tokens = re.split(r"[\s/]+", text)

        # ---- 1) 主控序列号前缀 (controller)，如 2517A0DRCR ----
        m = _RE_SERIAL_PREFIX.match(text)
        if m:
            dec = _decode_yyww(m.group(1), current_year)
            if dec:
                y, w = dec
                conf = 0.9 if _has_keyword(ctx, CONTROLLER_KEYWORDS) else 0.6
                results.append(DateCode(
                    raw=m.group(1), code_type="controller", year=y, week=w,
                    week_start=_week_start_date(y, w), confidence=conf,
                    source_text=text, box=box, digit_format="YYWW",
                ))
                continue  # 该 detection 已归类

        # ---- 2) SEC 534 形式（厂商 + 2~3 位）----
        msec = _RE_SEC_DATE.search(text)
        if msec:
            digits = msec.group(1)
            if len(digits) == 3:
                dec = _decode_yww(digits, current_year)
                if dec:
                    y, w = dec
                    results.append(DateCode(
                        raw=digits, code_type="dram", year=y, week=w,
                        week_start=_week_start_date(y, w), confidence=0.92,
                        source_text=text, box=box, digit_format="YWW",
                    ))
                    continue
            # 数字被反光/模糊读残(只剩2位)或非法：确属 SEC 日期行，挂起待补全
            pending.append(DateCode(
                raw=digits, code_type="dram", year=0, week=0, week_start="",
                confidence=0.5, source_text=text, box=box, digit_format="YWW",
                note="__pending__",
            ))
            continue

        # ---- 3) 纯 4 位 token ----
        pure4 = [t for t in tokens if _RE_PURE4.match(t)]
        handled = False
        for t in pure4:
            dec = _decode_yyww(t, current_year)
            if not dec:
                continue
            y, w = dec
            if _has_keyword(ctx, CONTROLLER_KEYWORDS):
                code_type, conf = "controller", 0.8
            else:
                code_type, conf = "pcb", 0.7
            results.append(DateCode(
                raw=t, code_type=code_type, year=y, week=w,
                week_start=_week_start_date(y, w), confidence=conf,
                source_text=text, box=box, digit_format="YYWW",
            ))
            handled = True
        if handled:
            continue

        # ---- 4) 纯 3 位 token：仅当紧邻 SEC/SAMSUNG 锚点才采信 ----
        #   合法颗粒日期码几乎总是 "SEC 534" 形式（步骤2已处理合并情况）。
        #   裸 3 位数字多为 2D 码/料号碎片(220/124/889…)，不加严格护栏会大量误报。
        if not _RE_PURE3.match(text.strip()):
            continue
        if not _has_adjacent_vendor(idx, detections, radius):
            continue
        dec = _decode_yww(text.strip(), current_year)
        if not dec:
            continue
        y, w = dec
        results.append(DateCode(
            raw=text.strip(), code_type="dram", year=y, week=w,
            week_start=_week_start_date(y, w), confidence=0.8,
            source_text=text, box=box, digit_format="YWW",
        ))

    from collections import Counter

    if correct:
        # 多数表决补全：被读残的 SEC 日期行(pending)按颗粒多数值补全。
        clean_dram = [c for c in results if c.code_type == "dram"]
        if pending and clean_dram:
            dom = Counter((c.year, c.week) for c in clean_dram).most_common(1)[0][0]
            dy, dw = dom
            for c in pending:
                c.year, c.week = dy, dw
                c.week_start = _week_start_date(dy, dw)
                c.confidence = 0.6
                c.note = f"由多数推断（原始模糊：SEC{c.raw}…）"
                results.append(c)

        # 多数表决：同一内存条的颗粒日期码通常高度一致；
        # 与多数明显不符的孤立值大概率是 OCR 误读（如 534 被读成 634），降权并标注。
        dram = [c for c in results if c.code_type == "dram"]
        if len(dram) >= 4:
            counts = Counter((c.year, c.week) for c in dram)
            top_key, top_n = counts.most_common(1)[0]
            for c in dram:
                n = counts[(c.year, c.week)]
                if (c.year, c.week) != top_key and n <= max(1, top_n // 5):
                    c.confidence = min(c.confidence, 0.4)
                    top = next(x for x in dram if (x.year, x.week) == top_key)
                    c.note = f"疑似 OCR 误读：与多数({top.description}×{top_n})不一致"
    else:
        # 原生模式：不预测、不校正——读残的 SEC 行原样作为 raw 输出
        for c in pending:
            c.status = "raw"
            c.note = "原始模糊，未能解码（未做校正）"
            results.append(c)

    # 排序：类型固定顺序 + 置信度降序
    type_order = {"pcb": 0, "controller": 1, "dram": 2, "unknown": 3}
    results.sort(key=lambda c: (type_order.get(c.code_type, 9), -c.confidence))
    return results


def summarize(codes: list[DateCode]) -> list[dict]:
    """按 (类型, 年, 周) 去重汇总，统计出现次数，便于前端展示概览。"""
    groups: dict[tuple, dict] = {}
    for c in codes:
        key = (c.code_type, c.year, c.week)
        g = groups.setdefault(key, {
            "code_type": c.code_type,
            "type_label": c.type_label,
            "year": c.year,
            "week": c.week,
            "week_start": c.week_start,
            "description": c.description,
            "count": 0,
            "max_confidence": 0.0,
        })
        g["count"] += 1
        g["max_confidence"] = max(g["max_confidence"], c.confidence)
    out = list(groups.values())
    type_order = {"pcb": 0, "controller": 1, "dram": 2, "unknown": 3}
    out.sort(key=lambda g: (type_order.get(g["code_type"], 9), -g["count"]))
    return out
