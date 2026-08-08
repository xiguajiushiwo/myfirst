# -*- coding: utf-8 -*-
"""方案实测：框源只跑 OCR **检测**(det)，读数全交大模型，逐颗并发。

为什么这么切分：
  - rec(识别) 要对每框各跑一次小图前向，一面 140+ 框 → 本机 CPU 82~115s，是瓶颈；
  - det(检测) 只跑一次整图前向，实测本机 16.3s/面，拿到全部文字框坐标；
  - 读数交大模型（上次逐颗裁图实测自一致 98.8~100%、零空读）。

一颗颗粒上有多行丝印(厂标/料号/日期)，det 会给出 3~5 个框。逐框调大模型是浪费，
所以先按空间邻近**把同一颗的框并成一个颗粒框**，再逐颗调一次。

另外单独处理**中部靠右的小芯片**(MPS PMIC MP8895F + 旁边 SOT 封装)：
它们位于条中部横向区域，字更小，单独放大裁图并用不同提示词读批号行。

跑两轮看自一致性 —— 按业务铁律，两轮不一致的颗粒只能算盲点转人工，
严禁按"多数/离群"抹掉。

用法：
    .venv\\Scripts\\python.exe tools\\det_vl_bench.py 0076 front
需先跑：tools\\det_boxes.py uploads/0076/front.jpg 0076 front
"""
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from PIL import Image  # noqa: E402

uid = sys.argv[1] if len(sys.argv) > 1 else "0076"
side = sys.argv[2] if len(sys.argv) > 2 else "front"
ROUNDS = int(os.environ.get("VL_ROUNDS", "2"))
WORKERS = int(os.environ.get("VL_WORKERS", "16"))

det_path = os.path.join("logs", f"{uid}_{side}_det.json")
if not os.path.isfile(det_path):
    print(f"缺 {det_path}，先跑 tools/det_boxes.py")
    sys.exit(1)
with open(det_path, encoding="utf-8") as f:
    det = json.load(f)
W, H = det["W"], det["H"]
print(f"det 框 {len(det['boxes'])} 个，det 耗时 {det['det_sec']}s")


# --- 1) 同一颗的多行框 → 合并成一个颗粒框 ------------------------------------
def aabb(b):
    xs = [p[0] for p in b]
    ys = [p[1] for p in b]
    return min(xs), min(ys), max(xs), max(ys)


# 颗粒宽度尺度：取框宽中位数作参考，同颗的行框横向重叠、纵向紧邻
ws = sorted(aabb(b["box"])[2] - aabb(b["box"])[0] for b in det["boxes"])
med_w = ws[len(ws) // 2]
# 同颗合并的纵向阈值。实测本图(tools/_diag_gap.py)：**同颗内**相邻丝印行的 y 中心间隔
# 是 9~27px，**相邻颗之间**是 72~100px，两簇分得很开。取 45px 落在空档中间。
# 注意别用 med_w*0.75(≈76px)：正好骑在颗间距下界上，会把相邻颗并成一颗
# —— 实测那样 343 框只并出 28 颗(应约 80)，裁图过大、空读 19/24。
GAP_Y = float(os.environ.get("MERGE_GAP_Y", "45"))
print(f"框宽中位数 {med_w:.0f}px，同颗纵向间隔阈值 {GAP_Y:.0f}px")

groups = []
for b in sorted(det["boxes"], key=lambda b: (b["slot"], aabb(b["box"])[1])):
    if not b["slot"]:
        continue                # 条外的框（托盘反光等）丢弃
    x0, y0, x1, y1 = aabb(b["box"])
    hit = None
    for g in groups:
        if g["slot"] != b["slot"]:
            continue
        gx0, gy0, gx1, gy1 = g["x0"], g["y0"], g["x1"], g["y1"]
        ox = min(x1, gx1) - max(x0, gx0)                  # 横向重叠量
        if ox > min(x1 - x0, gx1 - gx0) * 0.45 and (y0 - gy1) < GAP_Y and y1 > gy0 - GAP_Y:
            hit = g
            break
    if hit:
        hit["x0"], hit["y0"] = min(hit["x0"], x0), min(hit["y0"], y0)
        hit["x1"], hit["y1"] = max(hit["x1"], x1), max(hit["y1"], y1)
        hit["n"] += 1
    else:
        groups.append({"slot": b["slot"], "x0": x0, "y0": y0, "x1": x1, "y1": y1, "n": 1})

# 中部小芯片区：每根条纵向中段（PMIC 那一带）
for g in groups:
    g["cy_rel"] = ((g["y0"] + g["y1"]) / 2) / H
    g["kind"] = "small" if 0.40 <= g["cy_rel"] <= 0.56 else "dram"

groups.sort(key=lambda g: (g["slot"], g["y0"], g["x0"]))
n_small = sum(1 for g in groups if g["kind"] == "small")
print(f"合并后 {len(groups)} 颗（dram {len(groups) - n_small} / 中部小芯片 {n_small}）")
for s in sorted({g["slot"] for g in groups}):
    d = sum(1 for g in groups if g["slot"] == s and g["kind"] == "dram")
    m = sum(1 for g in groups if g["slot"] == s and g["kind"] == "small")
    print(f"  槽{s}: dram {d}  small {m}")

# --- 2) 裁图 -----------------------------------------------------------------
img = Image.open(det["image"]).convert("RGB")
import cv2  # noqa: E402
import numpy as np  # noqa: E402


def enhance(im, up=1):
    a = np.asarray(im.convert("L"))
    a = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(a)
    o = Image.fromarray(a).convert("RGB")
    if up > 1:
        o = o.resize((o.width * up, o.height * up), Image.LANCZOS)
    return o


crops = []
for g in groups:
    pad = med_w * (0.35 if g["kind"] == "dram" else 0.9)   # 小芯片多留边，字太小
    box = (max(0, int(g["x0"] - pad)), max(0, int(g["y0"] - pad)),
           min(W, int(g["x1"] + pad)), min(H, int(g["y1"] + pad)))
    c = img.crop(box)
    crops.append(enhance(c, up=2 if g["kind"] == "small" else 1))
os.makedirs(f"logs/_det_crops", exist_ok=True)
for i in (0, 1, len(crops) // 2, len(crops) - 1):
    crops[i].save(f"logs/_det_crops/{side}_{i}_{groups[i]['kind']}.png")

# --- 3) 逐颗调大模型 ----------------------------------------------------------
if not os.environ.get("DASHSCOPE_API_KEY"):
    print("缺 DASHSCOPE_API_KEY")
    sys.exit(1)

from app import metrics  # noqa: E402
from app.inspection.quality_inspect import _chat, _img_data_url  # noqa: E402

P_DRAM = """这是一颗服务器内存条上存储颗粒的特写。读出它丝印里的**日期码**。
日期码是 3 位数字 YWW：Y=年份末位，WW=周数(01~53)，例如 543 = 2025年第43周。
通常在 "SEC" 字样附近或料号行下方。
只返回这 3 位数字，不要其他任何文字。看不清就返回空字符串。"""

P_SMALL = """这是内存条中部的一颗小芯片特写（可能是 MPS 的电源管理芯片 PMIC，
或旁边的小型 SOT 封装）。请原样读出它上面的**所有丝印文字**，逐行输出，行间用 | 分隔。
看不清的字用 ? 代替。完全看不清就返回空字符串。不要解释、不要猜测补全。"""


def read_one(i):
    g, im = groups[i], crops[i]
    prompt = P_DRAM if g["kind"] == "dram" else P_SMALL
    try:
        ans = _chat([{"type": "text", "text": prompt},
                     {"type": "image_url", "image_url": {"url": _img_data_url(im)}}])
        t = ans.strip()
        if g["kind"] == "dram":
            m = re.search(r"\d{3}", t)
            return m.group(0) if m else ""
        return re.sub(r"\s+", " ", t)[:80]
    except Exception as e:  # noqa: BLE001
        return f"_ERR:{type(e).__name__}"


print(f"\n模型 = {os.environ.get('QWEN_VL_MODEL')}   轮数 = {ROUNDS}   并发 = {WORKERS}")
rounds = []
for r in range(ROUNDS):
    before = metrics.vl_usage()
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        got = list(ex.map(read_one, range(len(groups))))
    sec = time.perf_counter() - t0
    use = metrics.vl_usage_delta(before)
    dram_got = [v for g, v in zip(groups, got) if g["kind"] == "dram"]
    empty = sum(1 for v in dram_got if not v)
    err = sum(1 for v in got if str(v).startswith("_ERR"))
    print(f"  第{r + 1}轮：{sec:6.1f}s  颗粒空读 {empty}/{len(dram_got)}  出错 {err}  "
          f"token {use['total_tokens']:6d}  调用 {use['calls']}")
    rounds.append({"got": got, "sec": sec, "use": use})

# --- 4) 结果 ------------------------------------------------------------------
g0 = rounds[0]["got"]
print("\n--- 颗粒日期分布（第1轮，按槽）---")
for s in sorted({g["slot"] for g in groups}):
    vals = [v for g, v in zip(groups, g0) if g["slot"] == s and g["kind"] == "dram"]
    c = Counter(v for v in vals if v)
    print(f"  槽{s}({len(vals)}颗): {'  '.join(f'{k}×{v}' for k, v in c.most_common())}"
          f"{'  空读×' + str(sum(1 for v in vals if not v)) if any(not v for v in vals) else ''}")

print("\n--- 中部小芯片读数（第1轮）---")
for g, v in zip(groups, g0):
    if g["kind"] == "small":
        print(f"  槽{g['slot']} y={g['cy_rel']:.2f}: {v!r}")

if ROUNDS >= 2:
    a, b = rounds[0]["got"], rounds[1]["got"]
    di = [i for i, g in enumerate(groups) if g["kind"] == "dram"]
    same = sum(1 for i in di if a[i] == b[i])
    print(f"\n--- 两轮自一致（颗粒）：{same}/{len(di)} "
          f"({same / max(1, len(di)) * 100:.1f}%) ---")
    for i in di:
        if a[i] != b[i]:
            print(f"  不一致 槽{groups[i]['slot']} 第{i}颗: 轮1={a[i]!r} 轮2={b[i]!r}")

avg_sec = sum(r["sec"] for r in rounds) / max(1, ROUNDS)
avg_tok = sum(r["use"]["total_tokens"] for r in rounds) / max(1, ROUNDS)
print(f"\n===== {side} 单轮：det {det['det_sec']}s + 大模型 {avg_sec:.1f}s "
      f"= {det['det_sec'] + avg_sec:.1f}s，{avg_tok:.0f} token =====")
with open(f"logs/det_vl_{uid}_{side}.json", "w", encoding="utf-8") as f:
    json.dump({"groups": groups, "det_sec": det["det_sec"],
               "rounds": [{"sec": r["sec"], "use": r["use"], "got": r["got"]} for r in rounds]},
              f, ensure_ascii=False, indent=2)
print(f"明细 logs/det_vl_{uid}_{side}.json")
