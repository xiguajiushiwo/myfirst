# -*- coding: utf-8 -*-
"""探测：中部带按 0/90/180/270 四个朝向各 OCR 一遍，找 511 与 2543。

为什么要转朝向：目视确认（tools/_peek_sot2.py 存的图）PMIC 的丝印
MPS2531/MP8895F/T548U20/18-62 是**竖排**的，而 PCB 的 E3 2543 是**横排倒印**。
横排 det 框套不住竖排文字 —— 这正是整图 343 条检测里一个 MPS/511 都没有、
只有 'C1P28'/'MT2' 这类碎片的原因。

mid_chip_probe 里"放大2×"能读出 MPS2531 是侥幸（放大后竖排单字勉强连成框）。
本脚本用旋转正面硬碰：哪个朝向能稳定读出 511 / 2543，就按那个朝向做生产实现。

用法：
    .venv\\Scripts\\python.exe tools\\mid_rot_probe.py uploads/0076/front.jpg
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
os.environ.pop("DASHSCOPE_API_KEY", None)          # 纯 OCR

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import app.recognition.ocr_engine as oe  # noqa: E402

oe.configure(device="cpu", use_server_models=os.environ.get("OCR_SERVER_MODELS", "0") == "1",
             det_limit_side_len=int(os.environ.get("OCR_DET_SIDE_LEN", "1536")),
             tile_bands=1)

img_path = sys.argv[1] if len(sys.argv) > 1 else "uploads/0076/front.jpg"
img = Image.open(img_path).convert("RGB")
W, H = img.size

bgr = cv2.imread(img_path)
gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 120)
col = cv2.GaussianBlur(edges.mean(axis=0).astype(np.float32).reshape(1, -1),
                       (max(31, (W // 80) * 2 + 1), 1), 0).ravel()
on = col > col.max() * 0.22
bands, i = [], 0
while i < len(on):
    if on[i]:
        j = i
        while j < len(on) and on[j]:
            j += 1
        if j - i >= int(W * 0.04):
            bands.append((i, j))
        i = j
    else:
        i += 1
print(f"切条 → {len(bands)} 根: {bands}\n")

# 中部带：目视确认 PMIC 在 y≈1400~1520、PCB 丝印在 y≈1320~1360、
# 疑似 SOT 在 y≈1540~1600。取 1250~1650 全包住。
Y0, Y1 = 1250, 1650
UP = 2                          # mid_chip_probe 实测 2× 最好，3/4× 反而把行切碎
TARGETS = ("2531", "2534", "511", "2543", "MP8895", "T548", "18-62", "5KR", "8Y1")

engine = oe.get_engine()
print(f"引擎就绪（server={oe._config['use_server_models']}）  y={Y0}..{Y1}  放大{UP}×\n")

for si, (x0, x1) in enumerate(bands, 1):
    base = img.crop((x0, Y0, x1, Y1))
    base = base.resize((base.width * UP, base.height * UP), Image.LANCZOS)
    print(f"===== 槽{si}  裁图 {base.size} =====")
    for rot in (0, 90, 180, 270):
        im = base.rotate(-rot, expand=True) if rot else base
        t0 = time.perf_counter()
        dets = oe._predict_array(engine, np.asarray(im))
        sec = time.perf_counter() - t0
        texts = [d["text"] for d in dets if d.get("text", "").strip()]
        hit = [t for t in texts if any(k.lower() in t.lower() for k in TARGETS)]
        print(f"  转{rot:3d}°  {im.size}  {sec:5.1f}s  {len(texts):3d}条  命中: {hit}")
        print(f"      全部: {texts}")
    print()
