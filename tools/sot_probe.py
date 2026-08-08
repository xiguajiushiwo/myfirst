# -*- coding: utf-8 -*-
"""专测 SOT 那颗（511 / 8Y1 / 5KR）：单独裁小块、大倍数放大。

为什么要单独裁：bmp_probe 里把 PMIC 和 SOT 包在同一个框里跑，
结果 SOT 的 8Y1/5KH 读出来了，但作为日期的 511 那一行被切碎成
'51' + '5' + '1' —— 两颗紧邻，det 把七行丝印混在一起切。

SOT 位置（目视 logs/_peek_bmp2/s*_mid.png）：紧贴 PMIC 右侧、
几乎抵着条的右边缘，丝印竖排三行。所以取条最右侧一小条、大倍数放大。

变量：x 起点比例 × 放大倍数 × 朝向。用 bmp（大恒，色彩好）测，
因为海康那张 0076 上四朝向全军覆没、连目视都找不到这颗。

用法：
    .venv\\Scripts\\python.exe tools\\sot_probe.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
os.environ.pop("DASHSCOPE_API_KEY", None)          # 纯 OCR

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import app.recognition.ocr_engine as oe  # noqa: E402

oe.configure(device="cpu", use_server_models=os.environ.get("OCR_SERVER_MODELS", "0") == "1",
             det_limit_side_len=int(os.environ.get("OCR_DET_SIDE_LEN", "1536")),
             tile_bands=1)

img = Image.open("test_photos/Image_20260730120139657.bmp").convert("RGB")
W, H = img.size
BANDS = [(394, 1050), (1181, 1838), (1969, 2651), (2730, 3413)]
Y0, Y1 = 1450, 1700            # 目视 SOT 在 y≈1500~1620，上下留余量

os.makedirs("logs/_sot_probe", exist_ok=True)
engine = oe.get_engine()
print(f"引擎就绪  裁 y={Y0}..{Y1}\n")

# 只取条最右侧的窄条：0.86 起 = 把 PMIC 排除在外（PMIC 大致在 0.70~0.86）
CASES = [(0.86, 8), (0.86, 12), (0.90, 8), (0.90, 12)]

for si, (x0, x1) in enumerate(BANDS, 1):
    w = x1 - x0
    print(f"===== 槽{si} =====")
    for fx, up in CASES:
        xa = int(x0 + w * fx)
        raw = img.crop((xa, Y0, x1, Y1))
        im = raw.resize((raw.width * up, raw.height * up), Image.LANCZOS)
        for rot in (270, 90):
            r = im.rotate(-rot, expand=True)
            t0 = time.perf_counter()
            dets = oe._predict_array(engine, np.asarray(r))
            sec = time.perf_counter() - t0
            texts = [d["text"] for d in dets if d.get("text", "").strip()]
            got = any("511" in t.replace(" ", "") for t in texts)
            tag = "✓" if got else " "
            print(f"  {tag} x{fx:.2f} {up:2d}× 转{rot}°  {r.size}  {sec:4.1f}s "
                  f"{len(texts):2d}条  {texts}")
            if got:
                r.save(f"logs/_sot_probe/s{si}_{fx}_{up}x_{rot}.png")
        raw.resize((raw.width * 6, raw.height * 6), Image.LANCZOS).save(
            f"logs/_sot_probe/_look_s{si}_{fx}.png")
    print()
