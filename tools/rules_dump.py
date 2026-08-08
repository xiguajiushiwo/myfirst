# -*- coding: utf-8 -*-
"""跑一次规则识别，把**原始 OCR 检测**和**解析出的日期码**全量存盘。

本机 CPU 一面要 100s 左右，所以一次跑完把 detections 存进
logs/<uid>_<side>_dets.json，之后调解析规则可以直接复用、不必重跑 OCR。

用法：
    .venv\\Scripts\\python.exe tools\\rules_dump.py 0076 front
"""
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# 纯 OCR：屏蔽大模型兜底，否则耗时混入网络等待、也看不清规则本身的能力
os.environ.pop("DASHSCOPE_API_KEY", None)

import app.recognition.ocr_engine as oe  # noqa: E402

oe.configure(device=os.environ.get("OCR_DEVICE", "cpu"),
             use_server_models=os.environ.get("OCR_SERVER_MODELS", "0") == "1",
             det_limit_side_len=int(os.environ.get("OCR_DET_SIDE_LEN", "1536")),
             tile_bands=int(os.environ.get("OCR_TILE_BANDS", "1")))

uid = sys.argv[1] if len(sys.argv) > 1 else "0076"
side = sys.argv[2] if len(sys.argv) > 2 else "front"
img = os.path.join("uploads", uid, f"{side}.jpg")
dets_path = os.path.join("logs", f"{uid}_{side}_dets.json")
os.makedirs("logs", exist_ok=True)

print(f"图 = {img}   server模型 = {oe._config['use_server_models']}   "
      f"边长 = {oe._config['det_limit_side_len']}   分块 = {oe._config['tile_bands']}")

if os.path.isfile(dets_path):
    print(f"复用已有 {dets_path}（要重跑请删掉它）")
    with open(dets_path, encoding="utf-8") as f:
        d = json.load(f)
    dets, ocr_sec = d["dets"], d["ocr_sec"]
else:
    t0 = time.perf_counter()
    oe.get_engine()
    print(f"模型加载 {time.perf_counter() - t0:.1f}s")
    t0 = time.perf_counter()
    dets = oe.recognize(img)
    ocr_sec = time.perf_counter() - t0
    print(f"OCR(det+rec) {ocr_sec:.1f}s → {len(dets)} 条检测")
    with open(dets_path, "w", encoding="utf-8") as f:
        json.dump({"image": img, "ocr_sec": round(ocr_sec, 2), "dets": dets},
                  f, ensure_ascii=False)

print(f"\n=== 全部 OCR 文本（{len(dets)} 条，按 y 排序）===")
for d in sorted(dets, key=lambda d: (d["box"][0][1] if d.get("box") else 0)):
    b = d.get("box")
    xy = f"({int(b[0][0]):5d},{int(b[0][1]):5d})" if b else "(   ?,    ?)"
    print(f"  {xy} {d['score']:.2f}  {d['text']!r}")

print(f"\n=== 文本频次（前 40）===")
for t, n in Counter(d["text"] for d in dets).most_common(40):
    print(f"  {n:3d}×  {t!r}")

# --- 解析（不含大模型兜底、不含 _tight_digit_box，纯看规则）------------------
from app.recognition.date_parser import parse_detections  # noqa: E402

codes = parse_detections(dets, current_year=2026, correct=False)
print(f"\n=== 规则解析出 {len(codes)} 个日期码 ===")
by_type = Counter(c.code_type for c in codes)
print(f"  按类型: {dict(by_type)}")
for ct in ("dram", "pcb", "controller", "unknown"):
    cs = [c for c in codes if c.code_type == ct]
    if not cs:
        continue
    dist = Counter(f"{c.year}W{c.week}" for c in cs if c.week)
    print(f"\n  [{ct}] {len(cs)} 个   {'  '.join(f'{k}×{v}' for k, v in dist.most_common(8))}")
    for c in cs[:6]:
        print(f"      raw={c.raw!r} {c.year}W{c.week} conf={c.confidence:.2f} "
              f"src={c.source_text!r}")
print(f"\nOCR 耗时 {ocr_sec:.1f}s   检测明细 {dets_path}")
