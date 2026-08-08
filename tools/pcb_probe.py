# -*- coding: utf-8 -*-
"""探测：PCB 丝印窄带用什么预处理，OCR 才能读出 4 位日期。

目视已确认四根条的丝印都完整（tools/_peek_pcb.py 存的图）：
    槽1 'D4 2540'(橙)  槽2 'E3 2543'(白弱)  槽3 'G4 2543'(白最弱)  槽4 'F3 2543'(橙)
但整图 OCR 343 条检测里只有两个截断的 '543'/'325' —— 看得见不等于 OCR 读得出。

核心假设：**别用灰度**。白丝印/橙丝印在深绿阻焊上，
  - 灰度：绿阻焊的 G 分量很高，灰度值被拉起来，和白字差距被压小
  - 红通道：绿阻焊 R 分量低(暗)，白字 R 高、橙字 R 更高 → 两者对比度天然拉开
所以 R 通道应该显著优于灰度。本脚本就是来证伪/证实这一点的。

变量：通道(gray/R/R+CLAHE/gray+CLAHE) × 放大(4×/6×) × 朝向(180°必转)

用法：
    .venv\\Scripts\\python.exe tools\\pcb_probe.py uploads/0076/front.jpg
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
os.environ.pop("DASHSCOPE_API_KEY", None)          # 纯 OCR，不要大模型兜底混淆结论

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import app.recognition.ocr_engine as oe  # noqa: E402

oe.configure(device="cpu", use_server_models=os.environ.get("OCR_SERVER_MODELS", "0") == "1",
             det_limit_side_len=int(os.environ.get("OCR_DET_SIDE_LEN", "1536")),
             tile_bands=1)

img_path = sys.argv[1] if len(sys.argv) > 1 else "uploads/0076/front.jpg"
img = Image.open(img_path).convert("RGB")

BANDS = [(668, 1248), (1387, 1983), (2295, 2719), (3031, 3433)]
Y0, Y1 = 1285, 1385                 # 目视定的丝印带，上下已留余量
PAD_X = 30                          # 左右外扩，防丝印压在切条边界被截
EXPECT = {1: "2540", 2: "2543", 3: "2543", 4: "2543"}   # 目视读出的真值


def to_rgb(a):
    """单通道数组 → OCR 要的 3 通道图。"""
    return Image.fromarray(a).convert("RGB")


def prep(crop, chan, clahe):
    a = np.asarray(crop)
    if chan == "R":
        g = a[:, :, 0]              # 红通道：绿阻焊暗、白/橙丝印亮
    elif chan == "gray":
        g = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    else:                           # B 通道对照组
        g = a[:, :, 2]
    if clahe:
        g = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8)).apply(g)
    return to_rgb(g)


engine = oe.get_engine()
print(f"引擎就绪（server={oe._config['use_server_models']}）  丝印带 y={Y0}..{Y1}\n")

CASES = [(c, cl, up) for c in ("gray", "R", "B") for cl in (False, True) for up in (4, 6)]
os.makedirs("logs/_pcb_probe", exist_ok=True)
score = {k: 0 for k in {(c, cl, up) for c, cl, up in CASES}}

for si, (x0, x1) in enumerate(BANDS, 1):
    raw = img.crop((max(0, x0 - PAD_X), Y0, min(img.width, x1 + PAD_X), Y1)).rotate(180)
    want = EXPECT[si]
    print(f"===== 槽{si}  期望 {want}  裁图 {raw.size} =====")
    for chan, clahe, up in CASES:
        im = prep(raw, chan, clahe)
        im = im.resize((im.width * up, im.height * up), Image.LANCZOS)
        t0 = time.perf_counter()
        dets = oe._predict_array(engine, np.asarray(im))
        sec = time.perf_counter() - t0
        texts = [d["text"] for d in dets if d.get("text", "").strip()]
        ok = any(want in t.replace(" ", "") for t in texts)
        if ok:
            score[(chan, clahe, up)] += 1
        tag = "✓" if ok else " "
        print(f"  {tag} {chan:4s} CLAHE={'是' if clahe else '否'} {up}×  "
              f"{sec:4.1f}s {len(texts):2d}条  {texts}")
        if ok:
            im.save(f"logs/_pcb_probe/s{si}_{chan}_{int(clahe)}_{up}x.png")
    print()

print("=== 各方案命中数（满分 4）===")
for k, v in sorted(score.items(), key=lambda kv: -kv[1]):
    chan, clahe, up = k
    print(f"  {v}/4   {chan:4s} CLAHE={'是' if clahe else '否'} {up}×")
