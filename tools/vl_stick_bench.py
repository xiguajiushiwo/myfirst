# -*- coding: utf-8 -*-
"""纯大模型读日期实测（按根裁图，不跑任何 OCR）。

与 tools/vl_only_bench.py 的区别：那个脚本的框来自本地 OCR，等于 OCR 一次都没省；
这里**框源只用几何**（列向边缘密度切 4 根条，这一步实测很稳），然后把整根条发给
大模型，让它自己找出条上所有日期 —— 包括存储颗粒(3 位 YWW)和中部靠右的小芯片
(MPS PMIC MP8895F 等)。

为什么不逐颗裁：几何逐颗切格实测不可靠（光照不均 + 芯片/托盘同为暗色，
投影与轮廓法都切不准）。而按根裁本来就是项目里已验证的做法（外观质检就这么做）。

跑两轮看自一致性：同一根两轮读数不同 = 模型不稳，按业务铁律这类颗粒只能算盲点
转人工，不能当"离群值"抹掉。

用法：
    .venv\\Scripts\\python.exe tools\\vl_stick_bench.py uploads/0076/front.jpg
    VL_ROUNDS=2 VL_WORKERS=4 可覆盖
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

img_path = sys.argv[1] if len(sys.argv) > 1 else "uploads/0076/front.jpg"
ROUNDS = int(os.environ.get("VL_ROUNDS", "2"))
WORKERS = int(os.environ.get("VL_WORKERS", "4"))

if not os.environ.get("DASHSCOPE_API_KEY"):
    print("缺 DASHSCOPE_API_KEY")
    sys.exit(1)

# --- 1) 几何切 4 根条（唯一的本地视觉步骤，不含 OCR）--------------------------
t_geo = time.perf_counter()
bgr = cv2.imread(img_path)
H, W = bgr.shape[:2]
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
geo_sec = time.perf_counter() - t_geo
print(f"几何切条 {geo_sec:.2f}s → {len(bands)} 根: {bands}")

pil = Image.open(img_path).convert("RGB")
crops = []
for bx0, bx1 in bands:
    rows = cv2.GaussianBlur(edges[:, bx0:bx1].mean(axis=1).astype(np.float32).reshape(1, -1),
                            (31, 1), 0).ravel()
    ys = np.where(rows > rows.max() * 0.20)[0]
    pad = 12
    crops.append(pil.crop((max(0, bx0 - pad), max(0, int(ys[0]) - pad),
                           min(W, bx1 + pad), min(H, int(ys[-1]) + pad))))
os.makedirs("logs/_stick", exist_ok=True)
for i, c in enumerate(crops, 1):
    c.save(f"logs/_stick/slot{i}.jpg", quality=92)
    print(f"  槽{i} 裁图 {c.size}")

# --- 2) 提示词：让大模型自己找出这根条上所有日期 ------------------------------
PROMPT = """这是一根服务器内存条的照片（整根，竖向）。请找出并读出上面**所有**芯片的日期/批号丝印。

芯片分两类：
1. 存储颗粒：大方形黑色封装，成两列排列。丝印里日期是**3 位数字** YWW
   （Y=年份末位，WW=周，如 543 表示 2025 年第 43 周）。通常紧跟在 "SEC" 或料号行附近。
2. 中部靠右的小芯片：如 MPS 的 PMIC（丝印含 MP8895F / T548U20 / 18-62 这类），
   以及旁边的小型 SOT 封装。读出它们上面的**日期码或批号行**原文。

严格要求：
- **逐颗独立读**，不要因为大多数颗粒是同一个日期就把某颗"顺"成一样的。
  如果某一颗确实和别的不同，必须原样报出来 —— 找出不一致的那颗正是本次检测的目的。
- 看不清的颗粒，date 返回空字符串 ""，不要猜。
- position 用简明的位置描述（如 "上半区左列第1颗" / "下半区右列第3颗" / "中部PMIC"）。

只返回 JSON，格式：
{"dram": [{"position": "...", "date": "543", "raw": "丝印原文"}, ...],
 "small": [{"position": "...", "text": "读到的批号/日期行原文"}, ...]}"""

from app import metrics  # noqa: E402
from app.inspection.quality_inspect import _chat, _img_data_url  # noqa: E402


def read_stick(im):
    try:
        ans = _chat([{"type": "text", "text": PROMPT},
                     {"type": "image_url", "image_url": {"url": _img_data_url(im)}}])
        t = re.sub(r"^```(?:json)?|```$", "", ans.strip(), flags=re.MULTILINE).strip()
        i, j = t.find("{"), t.rfind("}")
        return json.loads(t[i:j + 1]) if i >= 0 else {}
    except Exception as e:  # noqa: BLE001
        return {"_err": f"{type(e).__name__}: {e}"}


from concurrent.futures import ThreadPoolExecutor  # noqa: E402

print(f"\n模型 = {os.environ.get('QWEN_VL_MODEL')}   轮数 = {ROUNDS}   并发 = {WORKERS}")
rounds = []
for r in range(ROUNDS):
    before = metrics.vl_usage()
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        got = list(ex.map(read_stick, crops))
    sec = time.perf_counter() - t0
    use = metrics.vl_usage_delta(before)
    nd = sum(len(g.get("dram") or []) for g in got)
    ns = sum(len(g.get("small") or []) for g in got)
    print(f"  第{r + 1}轮：{sec:6.1f}s  颗粒 {nd}  小芯片 {ns}  "
          f"token {use['total_tokens']:6d}  调用 {use['calls']}")
    rounds.append({"got": got, "sec": sec, "use": use})

# --- 3) 结果 ------------------------------------------------------------------
for si in range(len(crops)):
    print(f"\n===== 槽{si + 1} =====")
    for r, rd in enumerate(rounds, 1):
        g = rd["got"][si]
        if g.get("_err"):
            print(f"  轮{r} 失败: {g['_err']}")
            continue
        dram = [d.get("date", "") for d in (g.get("dram") or [])]
        from collections import Counter
        cnt = Counter(d for d in dram if d)
        blank = sum(1 for d in dram if not d)
        print(f"  轮{r}: {len(dram)} 颗，空读 {blank}，分布 "
              f"{'  '.join(f'{k}×{v}' for k, v in cnt.most_common())}")
        for s in (g.get("small") or []):
            print(f"        小芯片[{s.get('position', '?')}] = {s.get('text', '')!r}")
    if ROUNDS >= 2:
        a = [d.get("date", "") for d in (rounds[0]["got"][si].get("dram") or [])]
        b = [d.get("date", "") for d in (rounds[1]["got"][si].get("dram") or [])]
        if len(a) == len(b):
            same = sum(1 for x, y in zip(a, b) if x == y)
            print(f"  两轮自一致：{same}/{len(a)}")
            for k, (x, y) in enumerate(zip(a, b)):
                if x != y:
                    print(f"    不一致 第{k + 1}颗: 轮1={x!r} 轮2={y!r}")
        else:
            print(f"  ✗ 两轮**颗粒数都不同**({len(a)} vs {len(b)})，无法逐颗对齐")

avg_sec = sum(r["sec"] for r in rounds) / max(1, ROUNDS)
avg_tok = sum(r["use"]["total_tokens"] for r in rounds) / max(1, ROUNDS)
print(f"\n===== 单面单轮：几何 {geo_sec:.2f}s + 大模型 {avg_sec:.1f}s，"
      f"{avg_tok:.0f} token =====")
with open("logs/vl_stick_result.json", "w", encoding="utf-8") as f:
    json.dump({"image": img_path, "geo_sec": geo_sec,
               "rounds": [{"sec": r["sec"], "use": r["use"], "got": r["got"]} for r in rounds]},
              f, ensure_ascii=False, indent=2)
print("明细 logs/vl_stick_result.json")
