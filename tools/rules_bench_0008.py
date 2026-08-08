# -*- coding: utf-8 -*-
"""跑 uploads/<uid> 正反两面的规则识别，并拆出耗时构成，定位慢在哪。

用法：
    .venv\\Scripts\\python.exe tools\\rules_bench_0008.py 0008
    OCR_SERVER_MODELS=0 ... 轻量模型
"""
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# 纯 OCR 基准：屏蔽大模型兜底（否则耗时混入网络等待）
if os.environ.get("BENCH_KEEP_VL") != "1":
    os.environ.pop("DASHSCOPE_API_KEY", None)

import app.recognition.ocr_engine as oe  # noqa: E402

oe.configure(
    device=os.environ.get("OCR_DEVICE", "cpu"),
    use_server_models=os.environ.get("OCR_SERVER_MODELS", "1") == "1",
    det_limit_side_len=int(os.environ.get("OCR_DET_SIDE_LEN", "1536")),
    tile_bands=int(os.environ.get("OCR_TILE_BANDS", "3")),
)

# 给引擎的每次底层推理埋点，数清到底调了多少次 OCR
_calls = {"n": 0, "sec": 0.0}
_orig_predict = oe._predict_array


def _counted(engine, arr):
    t = time.perf_counter()
    try:
        return _orig_predict(engine, arr)
    finally:
        _calls["n"] += 1
        _calls["sec"] += time.perf_counter() - t


oe._predict_array = _counted

import app.recognition.region_ocr as ro  # noqa: E402

ro._predict_array = _counted   # region_ocr 里 from ... import 过来的同名引用也要替换

uid = sys.argv[1] if len(sys.argv) > 1 else "0008"
base = os.path.join("uploads", uid)

cfg = oe._config
print(f"目录 = {base}")
print(f"设备 = {oe._effective_device()}   server模型 = {cfg['use_server_models']}   "
      f"检测边长 = {cfg['det_limit_side_len']}   分块 = {cfg['tile_bands']}")
print(f"CPU 线程 = {os.environ.get('OCR_CPU_THREADS', '默认10')}\n")

t_engine = time.perf_counter()
oe.get_engine()                                  # 单独计模型加载，不混进识别耗时
print(f"模型加载 = {time.perf_counter() - t_engine:.1f}s\n")

print(f"{'面':<8} {'总耗时(s)':>10} {'OCR推理(s)':>11} {'OCR次数':>8} {'日期码':>7}  周次分布")
print("-" * 88)

grand = 0.0
for side in ("front", "back"):
    p = os.path.join(base, f"{side}.jpg")
    if not os.path.isfile(p):
        print(f"{side:<8} 缺图 {p}")
        continue
    _calls["n"], _calls["sec"] = 0, 0.0
    t0 = time.perf_counter()
    codes = ro.recognize_rules(p, current_year=2026, side=side)
    sec = time.perf_counter() - t0
    grand += sec
    dist = Counter(f"{c.year}W{c.week}" for c in codes if c.week)
    top = "  ".join(f"{k}×{v}" for k, v in dist.most_common(5))
    print(f"{side:<8} {sec:>10.1f} {_calls['sec']:>11.1f} {_calls['n']:>8} "
          f"{len(codes):>7}  {top}")

print("-" * 88)
print(f"正反合计 = {grand:.1f}s")
