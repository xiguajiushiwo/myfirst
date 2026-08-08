# -*- coding: utf-8 -*-
"""对一张图跑**规则识别**（不需要模板），打印读出的日期码与耗时。

用法：
    .venv\\Scripts\\python.exe tools\\rules_test.py test_photos/_shot_test.jpg
    .venv\\Scripts\\python.exe tools\\rules_test.py <图路径> back    # 指定面(front/back)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from app.recognition.region_ocr import recognize_rules  # noqa: E402

img = sys.argv[1] if len(sys.argv) > 1 else "test_photos/_shot_test.jpg"
side = sys.argv[2] if len(sys.argv) > 2 else "front"
if not os.path.isfile(img):
    print("图不存在：", img)
    sys.exit(1)

t0 = time.perf_counter()
codes = recognize_rules(img, current_year=2026, side=side)
sec = time.perf_counter() - t0

print(f"图 = {img}   面 = {side}")
print(f"OCR 耗时 = {sec:.2f}s")
print(f"读出日期码 = {len(codes)} 个\n")
from collections import Counter  # noqa: E402

for i, c in enumerate(codes, 1):
    print(f"  [{i:3d}] raw={c.raw!r:>7} 型={c.code_type:<10} "
          f"{c.year}-W{c.week:02d} 起={c.week_start} 格式={c.digit_format:<5} "
          f"conf={c.confidence:.2f} 状态={c.status}")

print("\n=== 按 (型, 年-周) 汇总 ===")
for (t, y, w), n in sorted(Counter((c.code_type, c.year, c.week) for c in codes).items()):
    print(f"  {t:<10} {y}-W{w:02d}  ×{n}")

weeks = sorted({(c.year, c.week) for c in codes if c.code_type == "dram"})
if len(weeks) > 1:
    print(f"\n注意：DRAM 出现 {len(weeks)} 个不同周次 {weeks} —— 按业务铁律需逐颗定位比较")

