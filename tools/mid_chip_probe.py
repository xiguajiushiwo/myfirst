# -*- coding: utf-8 -*-
"""探测：中部小芯片 + PCB 丝印，裁图放大后 OCR 能否读出日期。

背景：整图尺度下这些字太小，实测 343 条检测里 MPS2531 / 511 / 2543 一条都没有
（只读到 C1P28、K514E、MT2 这类碎片）。本脚本验证"裁中部窄带 + 放大 + 增强"
能否把召回救回来 —— 若能，就不必为此把全图换成慢得多的 server 模型。

对每根条的中部横带试多组参数（放大倍数 × 是否增强），打印读到的全部文本，
人工看 MPS2531 / 511 / 2543 是否出现。

用法：
    .venv\\Scripts\\python.exe tools\\mid_chip_probe.py uploads/0076/front.jpg
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
os.environ.pop("DASHSCOPE_API_KEY", None)          # 纯 OCR，屏蔽大模型兜底

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import app.recognition.ocr_engine as oe  # noqa: E402

SERVER = os.environ.get("OCR_SERVER_MODELS", "0") == "1"
oe.configure(device="cpu", use_server_models=SERVER,
             det_limit_side_len=int(os.environ.get("OCR_DET_SIDE_LEN", "1536")),
             tile_bands=1)

img_path = sys.argv[1] if len(sys.argv) > 1 else "uploads/0076/front.jpg"
img = Image.open(img_path).convert("RGB")
W, H = img.size

# 几何切条（纯 Canny，0.1s）
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
print(f"切条 → {len(bands)} 根: {bands}")

# 中部横带：从实拍看 PMIC / SOT / PCB 丝印都落在这一带
Y0, Y1 = int(H * 0.39), int(H * 0.56)
print(f"中部横带 y = {Y0}..{Y1}\n")


def enhance(im):
    a = np.asarray(im.convert("L"))
    a = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(a)
    return Image.fromarray(a).convert("RGB")


engine = oe.get_engine()
print(f"引擎就绪（server={SERVER}）\n")

TARGETS = ("2531", "511", "2543", "MP8895", "T548", "18-62", "MPS")
CASES = [(2, False), (2, True), (3, True), (4, True)]

for si, (x0, x1) in enumerate(bands, 1):
    crop0 = img.crop((x0, Y0, x1, Y1))
    print(f"===== 槽{si}  裁图 {crop0.size} =====")
    for up, enh in CASES:
        im = enhance(crop0) if enh else crop0
        im = im.resize((im.width * up, im.height * up), Image.LANCZOS)
        t0 = time.perf_counter()
        dets = oe._predict_array(engine, np.asarray(im))
        sec = time.perf_counter() - t0
        texts = [d["text"] for d in dets if d.get("text", "").strip()]
        hit = [t for t in texts if any(k.lower() in t.lower() for k in TARGETS)]
        print(f"  放大{up}× 增强{'是' if enh else '否'}  {im.size}  {sec:5.1f}s  "
              f"{len(texts)}条   命中目标: {hit}")
        print(f"      全部: {texts}")
    print()
