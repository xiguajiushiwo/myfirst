# -*- coding: utf-8 -*-
"""纯几何定位识别区：切条 → 颗粒 → PCB / PMIC / SOT。**不跑 OCR、不调大模型。**

由 `tools/vl_locate.py` 移植进服务层。为什么要它（相对模板模式）：
  模板是一堆固定归一化坐标，基于某一次拍摄标定，**托盘位置一变就滑框** ——
  已知问题里记着 slot1/slot3 的 PCB 日期被读成 2517/2628（应为 2543）就是滑框。
  几何定位每张图现算，不存坐标，所以不会滑。

三步：
  ① 切条：按绿阻焊的列占比找 4 根条（条是绿板、托盘是黑塑料）。
  ② 颗粒：条内深色贴片 → 行/列投影栅格切分（颗粒是规整 N 行 × 2 列）。
  ③ PCB / PMIC / SOT：PCB 纵向**锚在颗粒网格上**现算；PMIC / SOT 暂仍用条内固定比例。

与 tools/vl_locate.py 的差异：本模块只提供函数、不写文件不打印，
坐标一律返回**整图像素** (x0, y0, x1, y1)。
"""
from __future__ import annotations

import numpy as np
from PIL import Image


# ---------------------------------------------------------------- ① 切条

def green_mask(rgb: np.ndarray, vk: int = 121) -> np.ndarray:
    """绿阻焊掩膜。条是绿板、托盘是黑塑料 —— 按"绿"分比按"边缘密度"分稳得多。

    为什么不用 Canny 列投影：托盘有压花纹理，边缘密度和条差不多，
    实测在样图上切出 3~5 根、还会把托盘并进条里。绿色是条独有的。

    vk = 竖向闭运算核高。切条用很长的核（整列只要有一处绿就算条身），
    找颗粒用短核（否则会把颗粒也填成绿）。
    """
    import cv2
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
    import cv2
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
    import cv2
    g = rgb_band[:, :, 1].astype(np.int16)
    b = rgb_band[:, :, 2].astype(np.int16)
    gray = cv2.cvtColor(rgb_band, cv2.COLOR_RGB2GRAY)
    m = ((b >= g - 2) & (gray < 110) & (gray > 18)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN,
                         cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)))
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (1, 17)))


def find_particles(rgb_band: np.ndarray, bw: int) -> list:
    """条内找 DRAM 颗粒，返回条内相对坐标 [(x0,y0,x1,y1), ...]。

    走"先行后列"的栅格切分而不是连通域：颗粒是规整的 N 行 × 2 列，
    行/列投影的波谷比轮廓稳 —— 轮廓会因为某两颗之间掩膜粘连就退化成一大块。
    """
    m = chip_mask(rgb_band)
    # 行切分用 0.30 而不是更低的阈值：颗粒本体行占比在 0.28~0.71 间波动
    # （最上那行亮丝印字把占比压到 0.28），行间缝是 0.00。
    # 试过降到 0.12 想把首行字圈进来 —— 结果相邻行粘连，槽1/2 从 20 掉到 14/18。
    rows = _runs(m.mean(axis=1), 0.30, 80)
    if not rows:
        return []
    # 同槽颗粒尺寸一致 → 用行段高度的上四分位当"真实颗粒高"。
    # 被切掉首行字的行段偏矮，取高分位才不会被它们拉低。
    real_h = int(np.percentile([yb - ya for ya, yb in rows], 75))

    # 先把每行的列段算出来再统一处理 —— "宽单列该不该劈成两颗"
    # 要拿别的行的单颗宽度做参照，必须先看完所有行。
    percol = [(ya, yb, _runs(m[ya:yb].mean(axis=0), 0.50, int(bw * 0.18)))
              for ya, yb in rows]
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
    # 实测槽1 在图底边多出一行，裁到的是金手指和托盘边缘。
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
            for _y, v in rows_.items():
                if len(v) != 2:
                    continue
                a, c = sorted(v, key=lambda b: b[0])
                if abs((a[0] + a[2]) / 2 - lc) <= tol and abs((c[0] + c[2]) / 2 - rc) <= tol:
                    keep += [a, c]
            out = keep

    out.sort(key=lambda b: (b[1], b[0]))     # 沿条方向逐行、行内左→右
    return out


# ---------------------------------------------------------------- ③ 三个固定区

# 条内横向相对比例（fx0, fx1）+ 整图纵向比例（fy0, fy1）。
#
# 横向比例是 tools/_calib.py 量出来的，不是目视估的：在整条中部带里用 OCR 找到
#   日期码的实际像素位置，换算成条内比例 —— 槽1 的 2536 在 x 0.156~0.259、
#   槽2 的 2534 在 0.161~0.276，也就是**只占条宽的 12%**。
#   原先取整条宽有两个后果，都实测到了：
#     ① 框里 88% 是无关内容，增强后旁边料号 QRG1720HP 的 1720 会抢成日期
#     ② 框跨到相邻条 —— 按铁律，把邻条日期读到本条头上正是要防的漏判
#
# 已知局限：pmic / sot 的纵向仍是**整图固定比例**，会跟着托盘位置漂
#   （和模板同一个毛病）。要照 pcb 的办法改成锚颗粒网格，得先扫出这两处
#   日期码的真实像素位置才有换算依据 —— 目前只测过 PMIC 在 2× 放大下 4/4 读对，
#   没有坐标。所以这两项**暂留固定比例**，不假装它已经稳了。
FIXED = {
    "pcb":  (0.113, 0.319, None, None),      # 纵向锚颗粒网格，见 BAND_REL
    "pmic": (0.45, 1.00, 0.462, 0.593),
    "sot":  (0.865, 0.955, 0.501, 0.529),
}

# pcb 纵向**不用固定比例，锚在颗粒网格上现算**（tools/_calib3.py 验证）。
#
# 为什么改：固定比例本质是个小模板，会跟着托盘位置漂。实测四槽 PCB 日期码的
#   绝对 y 比例 0.4651/0.4677/0.4723/0.4773 —— **单调下移**（拍摄透视，与模板
#   漂移单调递增 −0.0171→0.0849 同源）。要框住四槽得开到 2.4 倍字高，
#   而框一开大旁边料号就进来了；原上限 0.4801 更是直接错过槽4。
#
# 颗粒网格是更好的锚：它每张图现算、80 个全检出，且和 PCB 同在一块板上，
#   透视位移**一起发生**，所以用"颗粒之间的相对位置"表达 PCB 位置时漂移互相抵消。
#   实测间隙内比例 0.098/0.103/0.102/0.100 —— 几乎不动，
#   所需跨度从 2.4 倍字高降到 1.1 倍。实测框高 91px → 49px，四槽仍全框住。
#
# 基准：中部带把颗粒分成上下两块，取"上块最低行的底边"到"下块最高行的顶边"。
# 值已含 30% 余量。
BAND_REL = {"pcb": (0.072, 0.212)}


def band_gap(particles: list) -> tuple | None:
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


# ---------------------------------------------------------------- 对外入口

def locate(img: Image.Image, want_bands: int = 4,
           tpl_fixed: dict | None = None) -> dict:
    """定位一整盘。返回

        {"size": [W, H],
         "slots": [{"slot": 1, "band": [x0, x1],
                    "particles": [[x0,y0,x1,y1], ...],     # 整图像素，逐行左→右
                    "fixed": {"pcb": [...], "pmic": [...], "sot": [...]}}, ...],
         "warn": ["..."]}

    坐标一律整图像素。`fixed` 里**可能缺 key** —— 颗粒不足定不出中部带间隙时
    pcb 框直接不出，让上层标盲点转人工，而不是回退到会漂的固定比例后读错。

    tpl_fixed：**人工标定的模板框**（混合模式用），形如
        {1: {"pcb": [x0,y0,x1,y1]}, 2: {...}}   槽号 1 起、整图归一化坐标
    给了就**优先用它**，不再按颗粒网格现算 —— 因为 PCB/主控 丝印对比度太低、
    自动定位不可靠（实测四槽只有一槽能被 OCR 扫到），而人工标一次就能长期用。
    颗粒仍然每张图现算（固定颗粒框换相机/挪托盘就全废）。
    """
    a = np.asarray(img.convert("RGB"))
    H, W = a.shape[:2]
    warn: list[str] = []

    bands = split_bands(a, want=want_bands)
    if len(bands) != want_bands:
        warn.append(f"切条得到 {len(bands)} 根（期望 {want_bands}），定位结果不可信，建议人工复核")

    slots = []
    for si, (x0, x1) in enumerate(bands, 1):
        bw = x1 - x0
        parts = find_particles(a[:, x0:x1], bw)
        gap = band_gap(parts)
        fixed = {}
        # 混合模式：有人工标定的模板框就直接用（整图归一化 → 像素），不走现算。
        tf = (tpl_fixed or {}).get(si) or {}
        for name, box in tf.items():
            fixed[name] = [int(box[0] * W), int(box[1] * H),
                           int(box[2] * W), int(box[3] * H)]
        for name, (fx0, fx1, fy0, fy1) in FIXED.items():
            if name in fixed:                 # 模板已给，别用现算覆盖
                continue
            rel = BAND_REL.get(name)
            if rel is not None:
                if gap is None:
                    warn.append(f"槽{si}：颗粒不足、定不出中部带间隙，{name} 框未出（转人工）")
                    continue
                up_bot, span = gap
                gy0, gy1 = int(up_bot + span * rel[0]), int(up_bot + span * rel[1])
            else:
                gy0, gy1 = int(H * fy0), int(H * fy1)
            fixed[name] = [int(x0 + bw * fx0), gy0, int(x0 + bw * fx1), gy1]
        slots.append({
            "slot": si, "band": [int(x0), int(x1)],
            "particles": [[int(x0 + p[0]), int(p[1]), int(x0 + p[2]), int(p[3])]
                          for p in parts],
            "fixed": fixed,
        })
    return {"size": [W, H], "slots": slots, "warn": warn}
