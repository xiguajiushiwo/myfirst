# -*- coding: utf-8 -*-
"""纯大模型逐颗读日期 实测：准确率 / 自一致性 / 耗时 / token 花费。

分两步（框源复用本地 OCR，一次性，之后不再跑 OCR）：
  1) 先跑一次本地规则识别，把每颗的框坐标与 OCR 读数存进 <uid>_boxes.json 作基准
  2) 对同一批框裁图，逐颗并发调 Qwen-VL 读两轮，与 OCR 读数三方对比

用法：
    .venv\\Scripts\\python.exe tools\\vl_only_bench.py 0008            # 缺基准会自动先建
    VL_ROUNDS=2 VL_WORKERS=16 ... 覆盖轮数/并发

关键看三个数：
  - 两轮自一致率：低 = 大模型读数不稳定，同一盘复检结果会变
  - 与 OCR 一致率：差异处需人工看图裁决谁对
  - 空读率：读不清时是否老实返回空（prompt 已要求），空 = 可标盲点转人工
"""
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from PIL import Image  # noqa: E402

uid = sys.argv[1] if len(sys.argv) > 1 else "0008"
ROUNDS = int(os.environ.get("VL_ROUNDS", "2"))
WORKERS = int(os.environ.get("VL_WORKERS", "16"))
base = os.path.join("uploads", uid)
boxes_path = os.path.join("logs", f"{uid}_boxes.json")


def build_baseline():
    """跑一次本地 OCR，存下每颗的框与读数作为基准（慢，只做一次）。"""
    import app.recognition.ocr_engine as oe
    oe.configure(device=os.environ.get("OCR_DEVICE", "cpu"),
                 use_server_models=os.environ.get("OCR_SERVER_MODELS", "1") == "1",
                 det_limit_side_len=int(os.environ.get("OCR_DET_SIDE_LEN", "1536")),
                 tile_bands=int(os.environ.get("OCR_TILE_BANDS", "1")))
    import app.recognition.region_ocr as ro
    out = {}
    for side in ("front", "back"):
        p = os.path.join(base, f"{side}.jpg")
        if not os.path.isfile(p):
            continue
        t0 = time.perf_counter()
        codes = ro.recognize_rules(p, current_year=2026, side=side)
        print(f"  {side}: 本地 OCR {time.perf_counter() - t0:.1f}s，{len(codes)} 个框")
        out[side] = [{"box": c.box, "raw": c.raw, "type": c.code_type,
                      "year": c.year, "week": c.week, "conf": c.confidence,
                      "idx": c.idx}
                     for c in codes if c.box]
    os.makedirs("logs", exist_ok=True)
    with open(boxes_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


if os.path.isfile(boxes_path):
    print(f"复用已有基准 {boxes_path}")
    with open(boxes_path, encoding="utf-8") as f:
        baseline = json.load(f)
else:
    print(f"首次运行：先建基准（本地 OCR，较慢）...")
    baseline = build_baseline()

from app import metrics  # noqa: E402
from app.inspection.quality_inspect import read_crop_vl  # noqa: E402
from app.recognition.region_ocr import _crop_region, _enhance  # noqa: E402

if not os.environ.get("DASHSCOPE_API_KEY"):
    print("缺 DASHSCOPE_API_KEY，无法测大模型")
    sys.exit(1)

print(f"\n轮数 = {ROUNDS}   并发 = {WORKERS}   模型 = {os.environ.get('QWEN_VL_MODEL')}")

report = {}
for side, items in baseline.items():
    p = os.path.join(base, f"{side}.jpg")
    img = Image.open(p).convert("RGB")
    # 裁图 + 对比增强。注意：不做 _prep_for_vl 的"翻正"，那步要跑 OCR，
    # 本实验的前提是不依赖 OCR；颗粒是贴片、方向由 SMT 固定，本就无需试旋转。
    crops, metas = [], []
    for it in items:
        c = _crop_region(img, it["box"])
        if c is None:
            continue
        crops.append(_enhance(c))
        metas.append(it)
    print(f"\n=== {side}：{len(crops)} 颗 ===")

    rounds = []
    for r in range(ROUNDS):
        before = metrics.vl_usage()
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            got = list(ex.map(lambda im: read_crop_vl(im, kind="dram"), crops))
        sec = time.perf_counter() - t0
        use = metrics.vl_usage_delta(before)
        empty = sum(1 for g in got if not g)
        print(f"  第{r + 1}轮：{sec:6.1f}s  空读 {empty:3d}/{len(got)}  "
              f"token {use['total_tokens']:6d}  调用 {use['calls']}")
        rounds.append({"got": got, "sec": sec, "use": use})

    # 自一致性：两轮读数是否相同
    if ROUNDS >= 2:
        a, b = rounds[0]["got"], rounds[1]["got"]
        same = sum(1 for x, y in zip(a, b) if x == y)
        both = [(x, y) for x, y in zip(a, b) if x and y]
        same_nonempty = sum(1 for x, y in both if x == y)
        print(f"  两轮自一致：{same}/{len(a)} ({same / max(1, len(a)) * 100:.1f}%)"
              f"   仅两轮都非空的：{same_nonempty}/{len(both)}")
        diff = [(m.get("idx"), m["raw"], x, y) for m, x, y in zip(metas, a, b) if x != y]
        for idx, ocr_raw, x, y in diff[:15]:
            print(f"    不一致 颗#{idx}: OCR={ocr_raw!r} 轮1={x!r} 轮2={y!r}")
        if len(diff) > 15:
            print(f"    ... 另有 {len(diff) - 15} 处不一致")

    # 与 OCR 对比（以第 1 轮为准）
    g = rounds[0]["got"]
    ocr_vs_vl_same = sum(1 for m, x in zip(metas, g) if m["raw"] == x)
    print(f"  与 OCR 读数一致：{ocr_vs_vl_same}/{len(g)} "
          f"({ocr_vs_vl_same / max(1, len(g)) * 100:.1f}%)")
    dist = Counter(x for x in g if x)
    print(f"  VL 读数分布(前6)：{'  '.join(f'{k}×{v}' for k, v in dist.most_common(6))}")
    ocr_dist = Counter(m["raw"] for m in metas if m["raw"])
    print(f"  OCR 读数分布(前6)：{'  '.join(f'{k}×{v}' for k, v in ocr_dist.most_common(6))}")

    report[side] = {"n": len(crops), "rounds": rounds}

tot_sec = sum(r["sec"] for s in report.values() for r in s["rounds"]) / max(1, ROUNDS)
tot_tok = sum(r["use"]["total_tokens"] for s in report.values() for r in s["rounds"]) / max(1, ROUNDS)
print(f"\n===== 单轮正反合计：{tot_sec:.1f}s   {tot_tok:.0f} token =====")
print(f"按 qwen-vl-ocr 0.3元/百万输入 估算 ≈ {tot_tok / 1e6 * 0.3:.4f} 元/盘"
      f"（当前实际用的是 {os.environ.get('QWEN_VL_MODEL')}，单价更高）")
