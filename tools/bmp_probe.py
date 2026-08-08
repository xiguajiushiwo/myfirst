# -*- coding: utf-8 -*-
"""在 test_photos 的 bmp 上实测：PMIC / SOT / PCB 三个区 OCR 能不能读出。

这张 bmp（7/30 拍）打光比 uploads/0076/front.jpg 亮得多、PCB 亮绿、丝印橙色，
目视四根条的 PMIC(MPS25xx)、SOT(511 8Y1 5KR)、PCB(25xx) 全清楚 —— 见
logs/_peek_bmp2/s{1,2}_mid.png。本脚本验证"目视清楚"能否转成"OCR 读得出"。

沿用 0076 上已证实的参数：
  - PMIC/SOT 竖排 → 裁窄带放大后转 270°（mid_rot_probe 实测四根全中）
  - PCB 弱对比   → 红通道、**不加** CLAHE、放大 4×（pcb_probe 实测 4/4，
                   加 CLAHE 掉到 1/4）
这张 PCB 是橙字亮底，红通道未必仍最优，所以 PCB 仍跑 gray/R/B 三通道对照。

用法：
    .venv\\Scripts\\python.exe tools\\bmp_probe.py
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

img = Image.open("test_photos/Image_20260730120139657.bmp").convert("RGB")
W, H = img.size

# 条边界目视量取（自动切条在这张上切出 5 根，失效）
BANDS = [(394, 1050), (1181, 1838), (1969, 2651), (2730, 3413)]
MID_Y = (1400, 1800)        # PMIC + SOT 所在中部带
PCB_Y = (1395, 1470)        # PCB 丝印在中部带最上缘（'2534 09-03' / '2536 E3'）

# 目视真值（logs/_peek_bmp2/*.png）
TRUTH = {1: {"pmic": "2523", "pcb": "2536"},
         2: {"pmic": "2522", "pcb": "2534"}}
KEYS = ("511", "8Y1", "5KR", "MPS25", "MP8895", "18-6")

engine = oe.get_engine()
print(f"引擎就绪（server={oe._config['use_server_models']}）  {W}×{H}\n")


def run(im, tag):
    t0 = time.perf_counter()
    dets = oe._predict_array(engine, np.asarray(im))
    sec = time.perf_counter() - t0
    texts = [d["text"] for d in dets if d.get("text", "").strip()]
    hit = [t for t in texts if any(k.lower() in t.lower().replace(" ", "") for k in KEYS)]
    print(f"    {tag:26s} {im.size}  {sec:4.1f}s {len(texts):2d}条  命中:{hit}")
    print(f"        全部: {texts}")
    return texts


print("########## 一、PMIC + SOT（中部带，竖排 → 转 270°）##########")
for si, (x0, x1) in enumerate(BANDS, 1):
    print(f"  === 槽{si} ===")
    # 靠右 45% 起（PMIC 与 SOT 都在这侧），放大 2×（3/4× 会把竖排切碎）
    xa = int(x0 + (x1 - x0) * 0.45)
    base = img.crop((xa, MID_Y[0], x1, MID_Y[1]))
    base = base.resize((base.width * 2, base.height * 2), Image.LANCZOS)
    for rot in (270, 90):
        run(base.rotate(-rot, expand=True), f"转{rot}°")

print("\n########## 二、PCB 丝印（横排，本张是正印不倒）##########")
for si, (x0, x1) in enumerate(BANDS, 1):
    print(f"  === 槽{si}  期望 {TRUTH.get(si, {}).get('pcb', '?')} ===")
    raw = img.crop((max(0, x0 - 20), PCB_Y[0], min(W, x1 + 20), PCB_Y[1]))
    for chan in ("gray", "R", "B"):
        a = np.asarray(raw)
        g = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY) if chan == "gray" else \
            a[:, :, 0] if chan == "R" else a[:, :, 2]
        im = Image.fromarray(g).convert("RGB")
        im = im.resize((im.width * 4, im.height * 4), Image.LANCZOS)
        run(im, f"{chan} 4×")
