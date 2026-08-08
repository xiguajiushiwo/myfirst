# -*- coding: utf-8 -*-
"""数清模板识别一盘到底调了多少次 OCR，并按用途归类。

为什么先数次数而不是先调参：
  CLAUDE.md 记 GPU 整盘 5.0s，而 GPU 峰值利用率只有 56%、显存净占 641MB ——
  结论是"瓶颈在逐框调度不在算力"。既然如此，能省的最大一块就是**调用次数本身**。
  但代码里一个框会走好几条路径（增强读 / 原图读 / 翻转读 / 收紧标注框重读），
  不插桩数不清，凭读代码估会漏。

做法：猴补丁 _predict_array 计数，按调用栈里的函数名归类用途，
      跑一次真实的 recognize_side，打印分类统计。

用法：
    .venv\\Scripts\\python.exe tools\\call_count.py <图路径> <front|back> [模板id]
"""
import collections
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
# 只测 OCR：把大模型 key 摘掉，免得兜底路径去调网络、把耗时算到 OCR 头上
os.environ.pop("DASHSCOPE_API_KEY", None)

import app.recognition.ocr_engine as oe  # noqa: E402

SRC = sys.argv[1] if len(sys.argv) > 1 else None
SIDE = sys.argv[2] if len(sys.argv) > 2 else "back"
TID = sys.argv[3] if len(sys.argv) > 3 else None
if not SRC:
    sys.exit("用法: call_count.py <图路径> <front|back> [模板id]")

oe.configure(device=os.environ.get("OCR_DEVICE", "gpu"),
             use_server_models=os.environ.get("OCR_SERVER_MODELS", "1") == "1",
             det_limit_side_len=int(os.environ.get("OCR_DET_SIDE_LEN", "1536")),
             tile_bands=1)

# ---- 插桩：按调用来源归类 ----
_orig = oe._predict_array
stats = collections.Counter()
times = collections.Counter()
# 归类关键字 → 用途（按栈由内到外第一个命中的算）
TAGS = [
    ("_tight_digit_box", "收紧标注框(可省)"),
    ("_prep_for_vl",     "大模型前判方向(可省)"),
    ("_eval_crop",       "读日期"),
]


def counted(engine, arr):
    stack = [f.name for f in traceback.extract_stack()]
    tag = "其他"
    for key, name in TAGS:
        if key in stack:
            tag = name
            break
    t0 = time.perf_counter()
    try:
        return _orig(engine, arr)
    finally:
        dt = time.perf_counter() - t0
        stats[tag] += 1
        times[tag] += dt


oe._predict_array = counted
# region_ocr 是 `from .ocr_engine import _predict_array` 直接绑进模块的，
# 只改 ocr_engine 上的名字它看不到 —— 必须把它模块里的引用也换掉。
import app.recognition.region_ocr as ro  # noqa: E402

ro._predict_array = counted

print(f"图 {SRC}  面 {SIDE}  设备 {oe._effective_device()}")
oe.get_engine()
print("引擎就绪，开始识别…\n")

t0 = time.perf_counter()
codes = ro.recognize_side(SRC, SIDE, template_id=TID)
wall = time.perf_counter() - t0

total = sum(stats.values())
print("=" * 60)
print(f"识别框数 {len(codes)}   OCR 调用 {total} 次   总墙钟 {wall:.1f}s")
print("=" * 60)
for tag, n in stats.most_common():
    print(f"  {tag:22s} {n:4d} 次   {times[tag]:6.1f}s   "
          f"({times[tag]/wall*100:4.1f}% 墙钟, 均 {times[tag]/n*1000:.0f}ms)")
saveable = sum(n for t, n in stats.items() if "可省" in t)
saved_t = sum(times[t] for t in stats if "可省" in t)
print("-" * 60)
print(f"  标记「可省」合计 {saveable} 次 / {saved_t:.1f}s "
      f"→ 去掉可省 {saved_t/wall*100:.0f}% 墙钟")
ok = sum(1 for c in codes if getattr(c, "week", 0))
print(f"\n读出 {ok}/{len(codes)}")
