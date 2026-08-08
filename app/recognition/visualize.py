"""在原图上绘制日期码识别框与标注。

按类型用不同颜色画框，并标注「类型 + 年第N周」。使用 PIL + 中文字体绘制中文标签。
"""
from __future__ import annotations

import os
from PIL import Image, ImageDraw, ImageFont

from .date_parser import DateCode

# 类型 -> 颜色 (RGB)
TYPE_COLORS = {
    "pcb": (255, 64, 64),        # 红
    "controller": (255, 170, 0),  # 橙
    "dram": (40, 200, 90),       # 绿
    # 几何定位模式新增两处。给独立色而不吃 unknown 的兜底灰 ——
    # 否则它们和"未分类"同色，图上四种框分不开，人工复核时看不出漏的是哪处。
    "pmic": (0, 160, 255),       # 蓝
    "sot": (220, 80, 255),       # 紫
    "unknown": (150, 150, 150),  # 灰
}

# 未解码时的展示色
RAW_COLOR = (255, 170, 0)        # 橙：原生读数（未能解码为年/周）
EMPTY_COLOR = (150, 150, 150)    # 灰：完全没读到文字


def _label_and_color(c, type_color):
    """决定标签文字与颜色。

    存储颗粒带序号前缀（如 "3.25年34周"），便于和不合格说明里的"第N颗"对应。
    解码成功 → 「YY年WW周」(类型色)；未解码 → 原样显示 OCR 原始读数(橙)；
    完全无读数 → 「未识别」(灰)。不做任何预测/校正。
    """
    pre = ""
    if c.code_type == "dram" and getattr(c, "idx", None):
        pre = f"{c.idx}."
    if c.week:
        return f"{pre}{c.year % 100}年{c.week}周", type_color
    # 读不出：原样展示 OCR 原始读数（错的也输出，不显示"遮挡"）；完全无读数才「未识别」
    raw = (getattr(c, "raw", "") or "").strip()
    if raw:
        return (pre + raw[:10], RAW_COLOR)
    return pre + "未识别", EMPTY_COLOR

# 中文字体候选（Windows 自带）
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _poly_bbox(box):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def annotate(image_path: str, codes: list[DateCode], out_path: str) -> str:
    """把识别到的日期码画到图上并保存，返回输出路径。"""
    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    # 字号随图片尺寸自适应
    scale = max(img.width, img.height) / 1000.0
    font = _load_font(max(16, int(22 * scale)))
    line_w = max(2, int(3 * scale))

    for c in codes:
        if not c.box:
            continue
        color = TYPE_COLORS.get(c.code_type, TYPE_COLORS["unknown"])
        # 画多边形框
        draw.line([tuple(p) for p in c.box] + [tuple(c.box[0])], fill=color, width=line_w)

        # 标签文字
        label = f"{c.type_label} | {c.year}W{c.week}"
        x0, y0, x1, y1 = _poly_bbox(c.box)
        tb = draw.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        # 标签放框上方，越界则放下方
        ly = y0 - th - 6
        if ly < 0:
            ly = y1 + 4
        draw.rectangle([x0, ly, x0 + tw + 8, ly + th + 6], fill=color)
        draw.text((x0 + 4, ly + 2), label, fill=(0, 0, 0), font=font)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)
    return out_path


def _rects_overlap(a, b, pad=2):
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or
                a[3] + pad < b[1] or b[3] + pad < a[1])


def _place_column(items, x_inner, side, top_guard, img_h, gap):
    """在某一侧边距把标签竖向堆叠，互不重叠。

    items: [(c, color, label, bw, bh, box_bbox)]，按目标 y 升序。
    side : "left" 标签右缘贴 x_inner；"right" 标签左缘贴 x_inner。
    返回 [(c, color, label, rect, anchor_xy)]，rect 已避让，必要时整体下推。
    """
    placed = []
    prev_bottom = top_guard + 2
    for c, color, label, bw, bh, bb in items:
        x0, y0, x1, y1 = bb
        cy = (y0 + y1) / 2
        ly = max(cy - bh / 2, prev_bottom)          # 不与上一个重叠
        ly = min(ly, img_h - bh - 2)
        if side == "left":
            lx = x_inner - bw
            anchor = (x0, cy)                         # 引到框左缘
        else:
            lx = x_inner
            anchor = (x1, cy)                         # 引到框右缘
        rect = [lx, ly, lx + bw, ly + bh]
        placed.append((c, color, label, rect, anchor))
        prev_bottom = ly + bh + gap
    return placed


_PANEL_BG = (236, 238, 242)   # 两侧扩展面板的浅灰底（与常见桌面背景接近，标签可见）


def _draw_empty_slots(draw, empty_slots, dx, dy, font, line_w):
    """把空槽画成灰框 + 「空位」，让质检员看到"这里本来没放条"（不静默跳过）。

    empty_slots：原图像素坐标矩形 [[x0,y0,x1,y1], ...]；dx/dy 为画布偏移。
    """
    if not empty_slots:
        return
    for x0, y0, x1, y1 in empty_slots:
        x0, y0, x1, y1 = x0 + dx, y0 + dy, x1 + dx, y1 + dy
        draw.rectangle([x0, y0, x1, y1], outline=EMPTY_COLOR, width=max(2, line_w))
        lbl = "空位"
        tb = draw.textbbox((0, 0), lbl, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        cx = (x0 + x1) / 2 - tw / 2
        cy = (y0 + y1) / 2 - th / 2
        draw.rectangle([cx - 6, cy - 4, cx + tw + 6, cy + th + 8], fill=EMPTY_COLOR)
        draw.text((cx, cy), lbl, fill=(255, 255, 255), font=font)


def annotate_clean(image_path: str, codes: list[DateCode], out_path: str,
                   title: str = "", empty_slots: list = None) -> str:
    """清晰标注：短标签全部移到内存条两侧，竖向堆叠互不重叠、绝不被裁切。

    关键：当照片里内存条贴边、两侧空白不够放标签时，**自动在画布两侧扩出面板**，
    保证每个标签都有完整空间，永远不会被截断或压在芯片上。
    每个标签用细引导线连回对应方框，一一对应、清晰可读。
    """
    base = Image.open(image_path).convert("RGB")
    W0, H0 = base.size
    scale = max(W0, H0) / 1000.0
    font = _load_font(max(15, int(19 * scale)))
    tfont = _load_font(max(18, int(26 * scale)))
    line_w = max(2, int(2.5 * scale))
    gap = max(6, int(7 * scale))
    margin_gap = max(10, int(14 * scale))

    measure = ImageDraw.Draw(base)
    top_guard = 0
    if title:
        tb = measure.textbbox((0, 0), title, font=tfont)
        top_guard = (tb[3] - tb[1]) + 16

    boxed = [c for c in codes if c.box]

    def _finish(canvas):
        d = ImageDraw.Draw(canvas)
        if title:
            d.rectangle([0, 0, canvas.width, top_guard], fill=(15, 20, 30))
            d.text((14, 8), title, fill=(255, 255, 255), font=tfont)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        canvas.save(out_path)
        return out_path

    if not boxed:
        canvas = Image.new("RGB", (W0, H0 + top_guard), _PANEL_BG)
        canvas.paste(base, (0, top_guard))
        _draw_empty_slots(ImageDraw.Draw(canvas), empty_slots, 0, top_guard, font, line_w)
        return _finish(canvas)

    # 1) 量每个标签 + 按原图坐标分左右
    items = []
    for c in boxed:
        type_color = TYPE_COLORS.get(c.code_type, TYPE_COLORS["unknown"])
        label, color = _label_and_color(c, type_color)
        tb = measure.textbbox((0, 0), label, font=font)
        bw, bh = (tb[2] - tb[0]) + 10, (tb[3] - tb[1]) + 8
        items.append([c, color, label, bw, bh, _poly_bbox(c.box)])

    mod_left0 = min(it[5][0] for it in items)
    mod_right0 = max(it[5][2] for it in items)
    mod_cx0 = (mod_left0 + mod_right0) / 2
    left0 = [it for it in items if (it[5][0] + it[5][2]) / 2 < mod_cx0]
    right0 = [it for it in items if (it[5][0] + it[5][2]) / 2 >= mod_cx0]
    maxbw_l = max((it[3] for it in left0), default=0)
    maxbw_r = max((it[3] for it in right0), default=0)

    # 2) 两侧空白不够则扩画布（只在不够时扩，居中条带则几乎不扩）
    pad_left = int(max(0, (maxbw_l + 2 * margin_gap) - mod_left0)) if left0 else 0
    pad_right = int(max(0, (maxbw_r + 2 * margin_gap) - (W0 - mod_right0))) if right0 else 0

    W, H = W0 + pad_left + pad_right, H0 + top_guard
    canvas = Image.new("RGB", (W, H), _PANEL_BG)
    canvas.paste(base, (pad_left, top_guard))
    draw = ImageDraw.Draw(canvas)
    if title:
        draw.rectangle([0, 0, W, top_guard], fill=(15, 20, 30))
        draw.text((14, 8), title, fill=(255, 255, 255), font=tfont)

    dx, dy = pad_left, top_guard

    # 3) 偏移坐标后画框
    for c, color, label, bw, bh, bb0 in items:
        type_color = TYPE_COLORS.get(c.code_type, TYPE_COLORS["unknown"])
        _, col = _label_and_color(c, type_color)
        sbox = [(p[0] + dx, p[1] + dy) for p in c.box]
        draw.line(sbox + [sbox[0]], fill=col, width=line_w)

    mod_left, mod_right = mod_left0 + dx, mod_right0 + dx
    mod_cx = (mod_left + mod_right) / 2
    left_items, right_items = [], []
    for c, color, label, bw, bh, bb0 in items:
        bb = (bb0[0] + dx, bb0[1] + dy, bb0[2] + dx, bb0[3] + dy)
        rec = (c, color, label, bw, bh, bb)
        (left_items if (bb[0] + bb[2]) / 2 < mod_cx else right_items).append(rec)
    left_items.sort(key=lambda r: (r[5][1] + r[5][3]) / 2)
    right_items.sort(key=lambda r: (r[5][1] + r[5][3]) / 2)

    placed = []
    if left_items:
        x_inner = max(2 + max(r[3] for r in left_items), mod_left - margin_gap)
        placed += _place_column(left_items, x_inner, "left", top_guard, H, gap)
    if right_items:
        x_inner = min(W - 2 - max(r[3] for r in right_items), mod_right + margin_gap)
        placed += _place_column(right_items, x_inner, "right", top_guard, H, gap)

    # 4) 画引导线 + 标签
    for c, color, label, rect, anchor in placed:
        lx, ly, rx, ry = rect
        cyc = (ly + ry) / 2
        side_x = rx if rect[0] < anchor[0] else lx
        draw.line([(anchor[0], anchor[1]), (side_x, cyc)], fill=color,
                  width=max(1, int(scale)))
        draw.rectangle(rect, fill=color)
        draw.text((lx + 5, ly + 4), label, fill=(0, 0, 0), font=font)

    _draw_empty_slots(draw, empty_slots, dx, dy, font, line_w)   # 空槽灰框 +「空位」

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    canvas.save(out_path)
    return out_path
