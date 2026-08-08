# -*- coding: utf-8 -*-
"""纯几何定位识别区：切条 → 颗粒框 → PMIC / SOT / PCB 框。**不跑 OCR、不调大模型。**

为什么要纯几何：
  这一轮的目标是"先定位、再分块、然后直接交多模态大模型读日期"，
  所以定位环节必须与 OCR 解耦 —— 否则又回到 build_template.py 的老路
  （靠先跑 OCR 反记框位，图暗时只找到 11 框、且检不出 PCB 倒印丝印）。

三步：
  ① 切条：列向边缘密度 + **合并相邻碎条**再取最宽 4 根。
     旧的 Canny 列投影在 test_photos 那张 bmp 上切出 5 根（把一根切两半），
     所以这里加了合并 + 取宽度前 4 的兜底。
  ② 颗粒：条内暗色矩形（DRAM 是深色贴片，阻焊是绿/亮）→ 阈值 + 形态学 + 轮廓，
     按面积/长宽比/占位过滤，再按行列排序。
  ③ PMIC / SOT / PCB：位置由条内相对坐标给（本会话已目视确认的相对位置），
     不靠检测 —— 这三处对比度低，检测不稳，但相对位置在同型号上是固定的。

用法：
    .venv\\Scripts\\python.exe tools\\vl_locate.py <图路径>
产物：
    logs/_vl/<stem>_定位.jpg   叠框图（人工核对用）
    logs/_vl/<stem>_regions.json  框坐标（供 vl_read.py 裁图）
"""
import json
import os
import sys

import cv2
import numpy as np
from PIL import Image

SRC = sys.argv[1] if len(sys.argv) > 1 else "test_photos/Image_20260730120139657.bmp"
OUT = "logs/_vl"
os.makedirs(OUT, exist_ok=True)
stem = os.path.splitext(os.path.basename(SRC))[0]


# ---------------------------------------------------------------- ① 切条
def green_mask(rgb: np.ndarray, vk: int = 121) -> np.ndarray:
    """绿阻焊掩膜。条是绿板、托盘是黑塑料 —— 按"绿"分比按"边缘密度"分稳得多。

    为什么不用 Canny 列投影（旧做法）：托盘有压花纹理，边缘密度和条差不多，
    在这张 bmp 上切出 3~5 根、还会把托盘并进条里。绿色是条独有的。

    vk = 竖向闭运算核高。切条用很长的核（整列只要有一处绿就算条身），
    找颗粒用短核（否则会把颗粒也填成绿）。
    """
    r = rgb[:, :, 0].astype(np.int16)
    g = rgb[:, :, 1].astype(np.int16)
    b = rgb[:, :, 2].astype(np.int16)
    m = ((g - r > 12) & (g - b > 12) & (g > 40)).astype(np.uint8) * 255
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (3, vk)))


def split_bands(rgb: np.ndarray, want: int = 4) -> list:
    """按绿阻焊的列占比找条。返回 [(x0,x1), ...]，最多 want 根，按 x 升序。

    关键是竖核要够长（601）：条中段整片都是颗粒、几乎不见绿，
    短核下列占比会掉到和条间缝一个量级，于是把一根条切成两半。
    长核把"这一列上任意位置有绿"传播成整列 → 条内 ≥0.12、条间缝 ≈0，
    绝对阈值 0.08 就能干净分开（实测四根与目视量取一致）。
    """
    H, W = rgb.shape[:2]
    m = green_mask(rgb, vk=601)
    col = cv2.GaussianBlur((m.mean(axis=0) / 255.0).astype(np.float32).reshape(1, -1),
                           (31, 1), 0).ravel()

    on = col > 0.08
    runs, i = [], 0
    while i < W:
        if on[i]:
            j = i
            while j < W and on[j]:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    runs = [r for r in runs if r[1] - r[0] >= int(W * 0.05)]
    runs.sort(key=lambda r: r[1] - r[0], reverse=True)
    return [(int(a), int(b)) for a, b in sorted(runs[:want])]


# ---------------------------------------------------------------- ② 颗粒
def _runs(sig: np.ndarray, thr: float, minlen: int) -> list:
    """一维信号里取连续超阈值段，短于 minlen 的丢掉。"""
    on = sig > thr
    out, i = [], 0
    n = len(on)
    while i < n:
        if on[i]:
            j = i
            while j < n and on[j]:
                j += 1
            if j - i >= minlen:
                out.append((i, j))
            i = j
        else:
            i += 1
    return out


def chip_mask(rgb_band: np.ndarray) -> np.ndarray:
    """DRAM 颗粒掩膜。返回 0/1 uint8。

    判据是**颜色**不是亮度：颗粒封装是深蓝紫（B ≳ G），绿阻焊是 G≫B，
    托盘黑塑料被 gray>18 排掉，亮焊盘/丝印被 gray<110 排掉。
    先试过"暗且非绿"，不行 —— 条内该掩膜占比 50~80%，颗粒间只剩极细绿缝，
    一做闭运算就全连成一片，连通域退化成整条。

    形态学只用**竖向**核：要桥接的是颗粒内那几行亮丝印字（~7px），
    而必须保住的是颗粒之间的列缝（~40px）。用方核会把左右两颗焊成一块。
    """
    g = rgb_band[:, :, 1].astype(np.int16)
    b = rgb_band[:, :, 2].astype(np.int16)
    gray = cv2.cvtColor(rgb_band, cv2.COLOR_RGB2GRAY)
    m = ((b >= g - 2) & (gray < 110) & (gray > 18)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)))
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 17)))


def find_particles(rgb_band: np.ndarray, bw: int, bh: int, tag: str = "") -> list:
    """条内找 DRAM 颗粒，返回条内相对坐标 [(x0,y0,x1,y1), ...]。

    走"先行后列"的栅格切分而不是连通域：颗粒是规整的 N 行 × 2 列，
    行/列投影的波谷比轮廓稳 —— 轮廓会因为某两颗之间掩膜粘连就退化成一大块。
    """
    m = chip_mask(rgb_band)
    if tag:
        cv2.imwrite(f"{OUT}/_mask_{tag}.png", m * 255)

    # 行切分用 0.30 而不是更低的阈值：颗粒本体行占比在 0.28~0.71 间波动
    # （最上那行亮丝印字把占比压到 0.28），行间缝是 0.00。
    # 试过降到 0.12 想把首行字圈进来 —— 结果相邻行粘连，槽1/2 从 20 掉到 14/18。
    # 所以保留干净的行切分，再在下面按颗粒真实高度**向上补齐**。
    rows = _runs(m.mean(axis=1), 0.30, 80)
    if not rows:
        return []
    # 同槽颗粒尺寸一致 → 用行段高度的上四分位当"真实颗粒高"。
    # 被切掉首行字的行段偏矮，取高分位才不会被它们拉低。
    real_h = int(np.percentile([yb - ya for ya, yb in rows], 75))

    # 先把每行的列段算出来，再统一处理 —— 因为"宽单列该不该劈成两颗"
    # 要拿别的行的单颗宽度做参照，必须先看完所有行。
    percol = [(ya, yb, _runs(m[ya:yb].mean(axis=0), 0.50, int(bw * 0.18)))
              for ya, yb in rows]
    # 单颗宽度基准：只信干净的双列行
    widths = [xb - xa for _, _, cs in percol if len(cs) == 2 for xa, xb in cs]
    one_w = float(np.median(widths)) if widths else 0.0

    out = []
    for ya, yb, cols in percol:
        # 颗粒是严格 2 列排布。>2 列 = 把托盘/金手指也算进来了，不可信、整行丢弃。
        if len(cols) > 2:
            continue
        # 只出 1 列有两种情形：① 两颗掩膜粘成一片 ② 只压到托盘边角。
        # 早前一律整行丢弃，代价是槽1 少了真实的一行（20→18）——
        # 而每一颗都必须看到（禁止多数表决），丢一行等于放过可能被偷换的那颗。
        # 所以按宽度区分：宽度接近两颗 → 从正中劈开；否则才丢。
        if len(cols) == 1:
            xa, xb = cols[0]
            if one_w and (xb - xa) >= one_w * 1.6:
                mid = (xa + xb) // 2
                gap = int(one_w * 0.04)          # 中缝留一点，避免两框贴死
                cols = [(xa, mid - gap), (mid + gap, xb)]
            else:
                continue
        # 只向上补：日期行在颗粒顶部，被切掉的总是上边
        ya2 = max(0, yb - real_h) if yb - ya < real_h else ya
        for xa, xb in cols:
            out.append((int(xa), int(ya2), int(xb), int(yb)))
    # 尺寸离群剔除：同盘颗粒尺寸一致，明显偏小/偏大的是托盘边角或粘连
    if len(out) >= 6:
        areas = np.array([(x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in out], dtype=np.float64)
        med = float(np.median(areas))
        out = [b for b, a in zip(out, areas) if 0.55 * med <= a <= 1.8 * med]

    # 列位置一致性过滤：颗粒是竖直两列，各行的左/右列中心几乎对齐
    # （沿条方向只有轻微透视偏移）。位置明显跳开的行不是颗粒 ——
    # 实测槽1 在图底边多出一行 y 2814..3036，裁到的是金手指和托盘边缘
    # （s1_dram19 里根本没有颗粒），它的左列中心偏了近一个颗粒宽。
    if len(out) >= 6:
        rows_ = {}
        for bx in out:
            rows_.setdefault(bx[1], []).append(bx)
        lefts = [min(v, key=lambda b: b[0]) for v in rows_.values() if len(v) == 2]
        rights = [max(v, key=lambda b: b[0]) for v in rows_.values() if len(v) == 2]
        if lefts and rights:
            lc = float(np.median([(b[0] + b[2]) / 2 for b in lefts]))
            rc = float(np.median([(b[0] + b[2]) / 2 for b in rights]))
            tol = float(np.median([b[2] - b[0] for b in out])) * 0.40
            keep = []
            for y, v in rows_.items():
                if len(v) != 2:
                    continue
                a, c = sorted(v, key=lambda b: b[0])
                if abs((a[0] + a[2]) / 2 - lc) <= tol and abs((c[0] + c[2]) / 2 - rc) <= tol:
                    keep += [a, c]
            out = keep

    out.sort(key=lambda b: (b[1], b[0]))     # 沿条方向逐行、行内左→右
    return out


# ---------------------------------------------------------------- ③ 三个固定区
# 条内相对坐标（fx0, fx1, fy0, fy1），比例 0~1。
# 来源：本会话 tools/_peek_bmp2.py + _peek_sot*.py 目视确认。
# PCB 丝印贴在中部带上缘、PMIC 在其下方靠右、SOT 紧贴 PMIC 右侧。
# SOT 是 PMIC 右侧那颗 8 脚 SOP，三行竖排丝印 511 / 8Y1 / 5KR，
# 行高仅 12~13px —— 本会话 OCR 32 组参数无一读出完整 511。
# 框位从 s2 的 PMIC 裁图反推：在 pmic 框(1482..1843, 1402..1800)内
# 位于 x 约 78%~88%、y 约 30%~51% → 换算成条内/图内比例如下。
# 横向比例是 tools/_calib.py 量出来的，不是目视估的：
#   在整条中部带里用 OCR 找到日期码的实际像素位置，换算成条内比例 ——
#   槽1 的 2536 在 x 0.156~0.259、槽2 的 2534 在 0.161~0.276，也就是**只占条宽的 12%**。
# 原值是 (0.00, 1.00)（整条宽），有两个后果，都实测到了：
#   ① 框里 88% 是无关内容，增强后旁边料号 QRG1720HP 的 1720 会抢成日期
#   ② 框跨到相邻条 —— 按铁律，把邻条日期读到本条头上正是要防的漏判
#
# 另一个实测发现：**四槽 PCB 丝印方向不一致**（槽1/2 正印、槽3/4 倒印 180°）。
#   CLAUDE.md 原记"PCB 板丝印是 180° 倒印的"，实际是同盘内两种方向并存 ——
#   所以 PCB 识别必须试两个方向，不能按固定方向处理。
FIXED = {
    "pcb":  (0.113, 0.319, None, None),      # 纵向锚颗粒网格，见 BAND_REL
    "pmic": (0.45, 1.00, 0.462, 0.593),
    "sot":  (0.865, 0.955, 0.501, 0.529),
}

# 纵向**不用固定比例，锚在颗粒网格上现算**（tools/_calib3.py 验证）。
#
# 为什么改：固定比例本质是个小模板，会跟着托盘位置漂。实测四槽 PCB 日期码的
#   绝对 y 比例 0.4651/0.4677/0.4723/0.4773 —— **单调下移**（拍摄透视，与模板
#   漂移单调递增 −0.0171→0.0849 同源）。要框住四槽得开到 2.4 倍字高，
#   而框一开大旁边料号就进来了；原上限 0.4801 更是直接错过槽4。
#
# 颗粒网格是更好的锚：它每张图现算、80 个全检出，且和 PCB 同在一块板上，
#   透视位移**一起发生**，所以用"颗粒之间的相对位置"表达 PCB 位置时漂移互相抵消。
#   实测间隙内比例 0.098/0.103/0.102/0.100 —— 几乎不动，
#   所需跨度从 2.4 倍字高降到 1.1 倍（收窄 2.2×）。
#
# 基准：中部带把颗粒分成上下两块，取"上块最低行的底边"到"下块最高行的顶边"。
# 值已含 30% 余量。
BAND_REL = {"pcb": (0.072, 0.212)}


def band_gap(particles: list) -> tuple:
    """从颗粒框找中部带间隙，返回 (上块底边 y, 间隙高度) 或 None。

    判据是"最大的行间空隙"：颗粒行之间的正常缝隙只有十几到几十 px，
    中部带有 350px 左右，量级差得很远，取最大那个不会误判。
    """
    ys = sorted({(p[1], p[3]) for p in particles})
    if len(ys) < 2:
        return None
    gaps = [(ys[i + 1][0] - ys[i][1], ys[i][1]) for i in range(len(ys) - 1)]
    gap, up_bot = max(gaps)
    if gap <= 0:
        return None
    return up_bot, gap


def main():
    img = Image.open(SRC).convert("RGB")
    W, H = img.size
    a = np.asarray(img)
    gray = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    print(f"图 {SRC}  {W}×{H}  亮度均值 {gray.mean():.1f}")

    bands = split_bands(a)
    print(f"切条 → {len(bands)} 根: {bands}")
    if len(bands) != 4:
        print("⚠ 未切出 4 根，后续裁图不可信，请核对叠框图")

    vis = a.copy()
    regions = {"src": SRC, "size": [W, H], "slots": []}

    for si, (x0, x1) in enumerate(bands, 1):
        bw, bh = x1 - x0, H
        parts = find_particles(a[:, x0:x1], bw, bh, tag=f"{stem}_s{si}")
        slot = {"slot": si, "band": [x0, x1], "particles": [], "fixed": {}}
        for pi, (px0, py0, px1, py1) in enumerate(parts, 1):
            slot["particles"].append({"i": pi, "box": [x0 + px0, py0, x0 + px1, py1]})
        gap = band_gap(parts)
        for name, (fx0, fx1, fy0, fy1) in FIXED.items():
            rel = BAND_REL.get(name)
            if rel is not None:
                # 纵向锚颗粒网格。找不到间隙就**不出这个框** ——
                # 宁可下游标盲点转人工，也不要回退到会漂的固定比例后读错。
                if gap is None:
                    print(f"  ⚠ 槽{si}: 颗粒不足、定不出中部带间隙，{name} 框跳过（转人工）")
                    continue
                up_bot, span = gap
                gy0 = int(up_bot + span * rel[0])
                gy1 = int(up_bot + span * rel[1])
            else:
                gy0, gy1 = int(H * fy0), int(H * fy1)
            slot["fixed"][name] = [int(x0 + bw * fx0), gy0,
                                   int(x0 + bw * fx1), gy1]
        regions["slots"].append(slot)
        print(f"  槽{si}: x {x0}..{x1}  颗粒 {len(parts)} 个")

        cv2.rectangle(vis, (x0, 0), (x1 - 1, H - 1), (0, 220, 220), 4)
        for p in slot["particles"]:
            bx0, by0, bx1, by1 = p["box"]
            cv2.rectangle(vis, (bx0, by0), (bx1, by1), (0, 230, 0), 3)
            cv2.putText(vis, str(p["i"]), (bx0 + 4, by0 + 34),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 230, 0), 3)
        for name, col in (("pcb", (0, 0, 255)), ("pmic", (255, 140, 0)), ("sot", (255, 0, 255))):
            if name not in slot["fixed"]:
                continue
            bx0, by0, bx1, by1 = slot["fixed"][name]
            cv2.rectangle(vis, (bx0, by0), (bx1, by1), col, 4)
            cv2.putText(vis, name, (bx0, max(24, by0 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, col, 3)

    Image.fromarray(vis).save(f"{OUT}/{stem}_定位.jpg", quality=88)
    with open(f"{OUT}/{stem}_regions.json", "w", encoding="utf-8") as f:
        json.dump(regions, f, ensure_ascii=False, indent=1)
    total = sum(len(s["particles"]) for s in regions["slots"])
    print(f"\n合计颗粒 {total} 个 + 每槽 3 个固定区")
    print(f"叠框图 → {OUT}/{stem}_定位.jpg")
    print(f"框坐标 → {OUT}/{stem}_regions.json")


main()
