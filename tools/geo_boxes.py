# -*- coding: utf-8 -*-
"""几何找框（完全不跑 OCR）：从整盘图里找出每根条上的芯片封装矩形。

为什么框源不能用 OCR：本次实验的前提就是"不依赖 OCR"。若框仍来自 PaddleOCR 的
检测模型，那 OCR 一次都没省下来（本机 CPU 上一盘 ~197s，正是要绕开的瓶颈）。

做法（两级投影，不用轮廓）：
  1) 列向**边缘密度**投影 → 切出 4 根条。
     不能用亮度均值：芯片本身是暗的，把整根条的均值压到和黑托盘差不多（实测找到 0 个带）。
  2) 条内做"暗块"行投影 → 切出芯片行；每行内再做列投影 → 切出该行的每一颗。
     不能用 Otsu 取暗块连通域：芯片暗、托盘也暗，一刀切下去轮廓糊成一个全尺寸大块
     （实测槽1/2 只得到 1 个 580×2620 的框）。投影法对这种规则网格稳。

产出：
  logs/<uid>_<side>_geo.json   每框 {box:[[x,y]x4], slot, kind, row}
  logs/<uid>_<side>_geo.jpg    叠框预览，供人工核对漏/多

kind（只按尺寸与位置分档，不看内容）：
  dram   规则网格里的大方封装 → 读 3 位 YWW
  small  中部靠右的小封装（MPS PMIC MP8895F、SOT23 等）→ 读批号/日期

用法：
    .venv\\Scripts\\python.exe tools\\geo_boxes.py uploads/0076/front.jpg 0076 front
"""
import json
import os
import sys
import time

import cv2
import numpy as np

img_path = sys.argv[1] if len(sys.argv) > 1 else "uploads/0076/front.jpg"
uid = sys.argv[2] if len(sys.argv) > 2 else "0076"
side = sys.argv[3] if len(sys.argv) > 3 else "front"
DEBUG = os.environ.get("GEO_DEBUG") == "1"

t0 = time.perf_counter()
bgr = cv2.imread(img_path)
H, W = bgr.shape[:2]
gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 120)


def _runs(mask, min_len):
    """把布尔序列里连续 True 的段落取出来（长度 >= min_len）。"""
    out, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            if j - i >= min_len:
                out.append((i, j))
            i = j
        else:
            i += 1
    return out


# --- 1) 切 4 根条：列向边缘密度 ------------------------------------------------
col = edges.mean(axis=0).astype(np.float32)
k = max(31, (W // 80) * 2 + 1)
col_s = cv2.GaussianBlur(col.reshape(1, -1), (k, 1), 0).ravel()
bands = _runs(col_s > col_s.max() * 0.22, int(W * 0.04))
print(f"切出 {len(bands)} 根条: {bands}")

# --- 2) 条内两级投影切芯片 ----------------------------------------------------
boxes = []
for slot, (bx0, bx1) in enumerate(bands, 1):
    band_w = bx1 - bx0
    # 条的上下边界（同样用边缘密度，避开托盘）
    rows_e = edges[:, bx0:bx1].mean(axis=1).astype(np.float32)
    rows_e = cv2.GaussianBlur(rows_e.reshape(1, -1), (31, 1), 0).ravel()
    ys = np.where(rows_e > rows_e.max() * 0.20)[0]
    by0, by1 = int(ys[0]), int(ys[-1])

    roi = gray[by0:by1, bx0:bx1]
    blur = cv2.GaussianBlur(roi, (5, 5), 0).astype(np.float32)
    # 先做**平场校正**再阈值。条上下光照不均（顶部明显偏暗），单一全局阈值会把
    # 整个暗区当成芯片（实测槽1 只切出 1 个 445 高的巨行）。
    # 除以大尺度背景后，比较的是"相对局部 PCB 有多暗"，与绝对亮度无关。
    # sigma 取 band_w*1.5：远大于芯片间距，只吃光照梯度，不会把芯片自己抹平。
    bg = cv2.GaussianBlur(blur, (0, 0), sigmaX=band_w * 1.5, sigmaY=band_w * 1.5)
    norm = blur / np.maximum(bg, 1e-3)
    dark = (norm < 0.80).astype(np.uint8) * 255
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    if DEBUG:
        cv2.imwrite(f"logs/_geo_dark_slot{slot}.png", dark)

    # 2a) 行投影 → 芯片行
    rp = dark.mean(axis=1)
    rp = cv2.GaussianBlur(rp.astype(np.float32).reshape(1, -1), (11, 1), 0).ravel()
    row_runs = _runs(rp > 60, int(band_w * 0.12))
    if DEBUG:
        print(f"  [调试]槽{slot} band={band_w}x{by1 - by0} "
              f"行段{len(row_runs)}个: {[(a, b - a) for a, b in row_runs][:16]}")

    for ri, (ry0, ry1) in enumerate(row_runs):
        strip = dark[ry0:ry1, :]
        rh = ry1 - ry0
        # 2b) 行内列投影 → 该行的每一颗
        cp = strip.mean(axis=0)
        cp = cv2.GaussianBlur(cp.astype(np.float32).reshape(1, -1), (11, 1), 0).ravel()
        for cx0, cx1 in _runs(cp > 60, int(band_w * 0.10)):
            w, h = cx1 - cx0, rh
            ar = w / max(1, h)
            if ar < 0.35 or ar > 4.0:
                continue
            gx, gy = cx0 + bx0, ry0 + by0
            # 大方封装 = dram；明显小的 = 中部小件
            kind = "dram" if (w > band_w * 0.28 and h > band_w * 0.22) else "small"
            boxes.append({"slot": slot, "kind": kind, "row": ri,
                          "box": [[int(gx), int(gy)], [int(gx + w), int(gy)],
                                  [int(gx + w), int(gy + h)], [int(gx), int(gy + h)]]})

sec = time.perf_counter() - t0
n_dram = sum(1 for b in boxes if b["kind"] == "dram")
print(f"\n几何找框 {sec:.2f}s：共 {len(boxes)} 框（dram {n_dram} / small {len(boxes) - n_dram}）")
for slot in sorted({b['slot'] for b in boxes}):
    d = sum(1 for b in boxes if b["slot"] == slot and b["kind"] == "dram")
    s = sum(1 for b in boxes if b["slot"] == slot and b["kind"] == "small")
    print(f"  槽{slot}: dram {d}  small {s}")

os.makedirs("logs", exist_ok=True)
out_json = os.path.join("logs", f"{uid}_{side}_geo.json")
with open(out_json, "w", encoding="utf-8") as f:
    json.dump({"image": img_path, "W": W, "H": H, "sec": round(sec, 3),
               "boxes": boxes}, f, ensure_ascii=False)

vis = bgr.copy()
for b in boxes:
    p = np.array(b["box"], np.int32)
    cv2.polylines(vis, [p], True,
                  (0, 255, 0) if b["kind"] == "dram" else (0, 128, 255), 4)
scale = 1500 / W
cv2.imwrite(os.path.join("logs", f"{uid}_{side}_geo.jpg"),
            cv2.resize(vis, (int(W * scale), int(H * scale))))
print(f"预览 logs/{uid}_{side}_geo.jpg   数据 {out_json}")
