# -*- coding: utf-8 -*-
"""框源：只跑 PaddleOCR 的**文本检测**（det），不跑识别（rec）。

思路：识别（rec）要对每个框各跑一次小图推理，一盘 140+ 框 ≈ 140 次前向，
是本机 CPU 上的主要成本（实测一面 82~115s）。检测只跑一次整图前向，
拿到所有文字框坐标就够了 —— 读数交给大模型。

产出：
  logs/<uid>_<side>_det.json   [{box, slot}]，已按槽和位置排好序
  logs/<uid>_<side>_det.jpg    叠框预览，供人工核对漏/多

用法：
    .venv\\Scripts\\python.exe tools\\det_boxes.py uploads/0076/front.jpg 0076 front
    DET_SIDE_LEN=1536 DET_BANDS=1 可覆盖
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

img_path = sys.argv[1] if len(sys.argv) > 1 else "uploads/0076/front.jpg"
uid = sys.argv[2] if len(sys.argv) > 2 else "0076"
side = sys.argv[3] if len(sys.argv) > 3 else "front"
SIDE_LEN = int(os.environ.get("DET_SIDE_LEN", "1536"))
BANDS = int(os.environ.get("DET_BANDS", "1"))
SERVER = os.environ.get("DET_SERVER", "0") == "1"

from paddleocr import TextDetection  # noqa: E402

name = "PP-OCRv5_server_det" if SERVER else None
proj_models = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
kw = dict(limit_side_len=SIDE_LEN, limit_type="max", device="cpu",
          thresh=0.2, box_thresh=0.3, unclip_ratio=2.0,
          # 必须关 mkldnn：paddle 3.3.1 的 oneDNN 新执行器缺
          # ConvertPirAttribute2RuntimeAttribute 对 ArrayAttribute<Double> 的实现，
          # CPU 上直接抛 NotImplementedError（同 ocr_engine._build_ocr 里的处理）。
          enable_mkldnn=os.environ.get("OCR_MKLDNN", "0") == "1")
if name:
    kw["model_name"] = name
    d = os.path.join(proj_models, name)
    if os.path.isdir(d):
        kw["model_dir"] = d

t_load = time.perf_counter()
det = TextDetection(**kw)
print(f"检测模型加载 {time.perf_counter() - t_load:.1f}s  "
      f"(server={SERVER}  side_len={SIDE_LEN}  bands={BANDS})")

bgr = cv2.imread(img_path)
H, W = bgr.shape[:2]
rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def run(arr):
    out = []
    for res in det.predict(input=arr):
        polys = res["dt_polys"] if "dt_polys" in res else res.get("dt_polys")
        for p in (polys if polys is not None else []):
            out.append(np.asarray(p).reshape(-1, 2).astype(float).tolist())
    return out


t0 = time.perf_counter()
polys = run(rgb)
if BANDS >= 2:                                    # 分块提升密集小字召回
    overlap = 0.18
    bh = int(H / (BANDS - (BANDS - 1) * overlap))
    step = max(1, int(bh * (1 - overlap)))
    y = 0
    while y < H:
        y2 = min(H, y + bh)
        for p in run(rgb[y:y2, :, :]):
            polys.append([[x, yy + y] for x, yy in p])
        if y2 >= H:
            break
        y += step
det_sec = time.perf_counter() - t0
print(f"检测(det only) {det_sec:.1f}s → {len(polys)} 个文字框")


# --- 简单 NMS 去重（分块会重复检出）------------------------------------------
def aabb(p):
    xs = [q[0] for q in p]
    ys = [q[1] for q in p]
    return min(xs), min(ys), max(xs), max(ys)


def iou(a, b):
    ax0, ay0, ax1, ay1 = aabb(a)
    bx0, by0, bx1, by1 = aabb(b)
    iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    if inter <= 0:
        return 0.0
    return inter / ((ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter + 1e-6)


kept = []
for p in sorted(polys, key=lambda p: -(aabb(p)[2] - aabb(p)[0]) * (aabb(p)[3] - aabb(p)[1])):
    if not any(iou(p, k) >= 0.35 for k in kept):
        kept.append(p)
print(f"去重后 {len(kept)} 框")

# --- 按条（槽）归属：沿用几何切条，纯 Canny，0.1s ------------------------------
gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 120)
col = cv2.GaussianBlur(edges.mean(axis=0).astype(np.float32).reshape(1, -1),
                       (max(31, (W // 80) * 2 + 1), 1), 0).ravel()
on = col > col.max() * 0.22
bands_x, i = [], 0
while i < len(on):
    if on[i]:
        j = i
        while j < len(on) and on[j]:
            j += 1
        if j - i >= int(W * 0.04):
            bands_x.append((i, j))
        i = j
    else:
        i += 1
print(f"几何切条 → {len(bands_x)} 根: {bands_x}")

boxes = []
for p in kept:
    x0, y0, x1, y1 = aabb(p)
    cx = (x0 + x1) / 2
    slot = 0
    for si, (bx0, bx1) in enumerate(bands_x, 1):
        if bx0 <= cx <= bx1:
            slot = si
            break
    boxes.append({"slot": slot, "box": [[int(round(q[0])), int(round(q[1]))] for q in p],
                  "cx": cx, "cy": (y0 + y1) / 2, "w": x1 - x0, "h": y1 - y0})
boxes.sort(key=lambda b: (b["slot"], b["cy"], b["cx"]))
for si in sorted({b["slot"] for b in boxes}):
    n = sum(1 for b in boxes if b["slot"] == si)
    print(f"  槽{si if si else '(条外)'}: {n} 框")

os.makedirs("logs", exist_ok=True)
out_json = os.path.join("logs", f"{uid}_{side}_det.json")
with open(out_json, "w", encoding="utf-8") as f:
    json.dump({"image": img_path, "W": W, "H": H, "det_sec": round(det_sec, 3),
               "boxes": boxes}, f, ensure_ascii=False)

vis = bgr.copy()
for b in boxes:
    cv2.polylines(vis, [np.array(b["box"], np.int32)], True,
                  (0, 255, 0) if b["slot"] else (0, 0, 255), 3)
s = 1500 / W
cv2.imwrite(os.path.join("logs", f"{uid}_{side}_det.jpg"),
            cv2.resize(vis, (int(W * s), int(H * s))))
print(f"预览 logs/{uid}_{side}_det.jpg   数据 {out_json}")
