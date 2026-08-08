# -*- coding: utf-8 -*-
"""CPU 下 OCR 参数扫描：找"读出率够 + 耗时可接受"的组合。

用法：
    .venv\\Scripts\\python.exe tools\\ocr_bench_cpu.py test_photos/_shot_test.jpg

每组合重开引擎（配置只在首次初始化生效），故各组合独立计时。
"""
import importlib
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# 只测**纯 OCR**：清掉 key 让大模型兜底自动跳过，否则耗时里混进网络等待
os.environ.pop("DASHSCOPE_API_KEY", None)

img = sys.argv[1] if len(sys.argv) > 1 else "test_photos/_shot_test.jpg"
result_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "logs", "ocr_bench_result.json")
results = {"image": os.path.abspath(img), "cases": []}
with open(result_path, "w", encoding="utf-8") as f:
    json.dump({**results, "status": "running"}, f, ensure_ascii=False, indent=2)

# 默认只测笔记本 CPU 可接受的轻量组合，避免误跑 server 模型长时间占满 CPU。
# OCR_BENCH_ALL=1 才扫描更多轻量组合；OCR_BENCH_SERVER=1 才允许测试 server 模型。
CASES = [("轻量 + 1536 + 1块", False, 1536, 1)]
if os.environ.get("OCR_BENCH_ALL") == "1":
    CASES += [("轻量 + 960 + 1块", False, 960, 1),
              ("轻量 + 1536 + 3块", False, 1536, 3)]
if os.environ.get("OCR_BENCH_SERVER") == "1":
    CASES += [("server + 1536 + 1块", True, 1536, 1)]

print(f"图 = {img}\n")
print(f"{'组合':<24} {'耗时(s)':>9} {'日期码数':>9} {'Token':>9}  周次分布")
print("-" * 78)

for label, server, side_len, bands in CASES:
    # 引擎是模块级单例，换配置必须重载模块
    import app.recognition.ocr_engine as oe
    import app.recognition.region_ocr as ro
    from app import metrics
    importlib.reload(oe)
    importlib.reload(ro)
    oe.configure(device="cpu", use_server_models=server,
                 det_limit_side_len=side_len, tile_bands=bands)
    t0 = time.perf_counter()
    token_before = metrics.vl_usage()
    try:
        codes = ro.recognize_rules(img, current_year=2026, side="front")
    except Exception as e:  # noqa: BLE001
        print(f"{label:<24} {'FAIL':>9}  {type(e).__name__}: {e}")
        results["cases"].append({"label": label, "ok": False,
                                 "error": f"{type(e).__name__}: {e}"})
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump({**results, "status": "failed"}, f, ensure_ascii=False, indent=2)
        continue
    sec = time.perf_counter() - t0
    token_usage = metrics.vl_usage_delta(token_before)
    dist = Counter(f"{c.year}W{c.week}" for c in codes)
    top = "  ".join(f"{k}×{v}" for k, v in dist.most_common(4))
    print(f"{label:<24} {sec:>9.1f} {len(codes):>9} "
          f"{token_usage['total_tokens']:>9}  {top}")
    print("token_usage =", token_usage)
    results["cases"].append({
        "label": label,
        "ok": True,
        "elapsed_sec": round(sec, 3),
        "code_count": len(codes),
        "week_distribution": dict(dist),
        "token_usage": token_usage,
    })
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump({**results, "status": "complete"}, f, ensure_ascii=False, indent=2)
