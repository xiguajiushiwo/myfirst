# -*- coding: utf-8 -*-
"""把已识别的结果画成框图：条边界 + 颗粒日期框 + PMIC 区 + PCB 丝印区。

数据来源（都是本会话实测存下来的，不重跑 OCR）：
  - logs/<uid>_<side>_dets.json  ← tools/rules_dump.py 存的 343 条整图检测
  - 条边界 / PMIC / PCB 的 y 区间 ← tools/_peek_sot*.py 目视 + mid_rot_probe 实测

颜色约定：
  青  条边界（Canny 列投影切出的 4 根）
  绿  颗粒日期（规则已解析出 year/week 的）
  黄  OCR 读到但规则没解析成日期的框
  橙  PMIC 区（竖排丝印，需转 270° 才读得出）
  红  PCB 丝印区（横排倒印，需转 180°/270°）

用法：
    .venv\\Scripts\\python.exe tools\\draw_boxes.py 0076 front
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

uid = sys.argv[1] if len(sys.argv) > 1 else "0076"
side = sys.argv[2] if len(sys.argv) > 2 else "front"
img_path = os.path.join("uploads", uid, f"{side}.jpg")
dets_path = os.path.join("logs", f"{uid}_{side}_dets.json")

with open(dets_path, encoding="utf-8") as f:
    dets = json.load(f)["dets"]

from app.recognition.date_parser import parse_detections  # noqa: E402

codes = parse_detections(dets, current_year=2026, correct=False)

img = Image.open(img_path).convert("RGB")
W, H = img.size
d = ImageDraw.Draw(img)


def font(sz):
    for n in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(n, sz)
        except OSError:
            continue
    return ImageFont.load_default()


F_S, F_M, F_L = font(22), font(30), font(46)

BANDS = [(668, 1248), (1387, 1983), (2295, 2719), (3031, 3433)]
# 目视确认（tools/_peek_sot*.py）：PCB 丝印 y≈1320~1360 横排倒印；
# PMIC(MPS2531 那颗) y≈1395~1525 竖排。
PCB_Y = (1315, 1365)
PMIC_Y = (1390, 1530)


def aabb(b):
    xs = [p[0] for p in b]
    ys = [p[1] for p in b]
    return min(xs), min(ys), max(xs), max(ys)


# --- 1) 已解析成日期的框（绿） ------------------------------------------------
dated = {id(c.box): c for c in codes if c.box}
drawn = set()
for c in codes:
    if not c.box:
        continue
    x0, y0, x1, y1 = aabb(c.box)
    d.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], outline=(0, 255, 60), width=3)
    d.text((x0, y1 + 2), f"{c.year % 100:02d}W{c.week:02d}", fill=(0, 255, 60), font=F_S)
    drawn.add((round(x0), round(y0)))

# --- 2) 其余 OCR 框（黄，细线） ------------------------------------------------
for det in dets:
    b = det.get("box")
    if not b or not det.get("text", "").strip():
        continue
    x0, y0, x1, y1 = aabb(b)
    if (round(x0), round(y0)) in drawn:
        continue
    d.rectangle([x0, y0, x1, y1], outline=(255, 210, 0), width=1)

# --- 3) 条边界（青）+ PMIC（橙）+ PCB（红） -----------------------------------
for si, (x0, x1) in enumerate(BANDS, 1):
    d.rectangle([x0, 40, x1, H - 40], outline=(0, 230, 255), width=4)
    d.text((x0 + 8, 48), f"槽{si}", fill=(0, 230, 255), font=F_L)

    d.rectangle([x0 + 4, PCB_Y[0], x1 - 4, PCB_Y[1]], outline=(255, 40, 40), width=4)
    d.text((x0 + 8, PCB_Y[1] + 4), "PCB 倒印", fill=(255, 40, 40), font=F_M)

    xa = int(x0 + (x1 - x0) * 0.62)
    d.rectangle([xa, PMIC_Y[0], x1 - 4, PMIC_Y[1]], outline=(255, 140, 0), width=4)
    d.text((x0 + 8, PMIC_Y[0] - 34), "PMIC 竖排", fill=(255, 140, 0), font=F_M)

# --- 4) 图例 ------------------------------------------------------------------
LEG = [((0, 230, 255), "条边界（Canny 切出 4 根）"),
       ((0, 255, 60), f"颗粒日期 已解析 {len(codes)} 个"),
       ((255, 210, 0), f"OCR 读到但非日期 {len(dets) - len(codes)} 框"),
       ((255, 140, 0), "PMIC 竖排 MPS2531（需转 270°）"),
       ((255, 40, 40), "PCB 倒印 E3 2543（需转 180/270°）")]
d.rectangle([20, H - 300, 900, H - 20], fill=(0, 0, 0))
for i, (c, t) in enumerate(LEG):
    y = H - 285 + i * 52
    d.rectangle([36, y, 86, y + 34], fill=c)
    d.text((100, y), t, fill=(255, 255, 255), font=F_M)

os.makedirs("logs", exist_ok=True)
out = f"logs/{uid}_{side}_框图.jpg"
img.save(out, quality=88)
print(f"框图 → {out}   ({W}×{H})")

# 顺带按槽统计
print(f"\n=== 按槽统计 ===")
for si, (x0, x1) in enumerate(BANDS, 1):
    cs = [c for c in codes if c.box and x0 <= aabb(c.box)[0] <= x1]
    from collections import Counter
    dist = Counter(f"{c.year % 100:02d}W{c.week:02d}" for c in cs)
    print(f"  槽{si}: {len(cs):2d} 个日期  {'  '.join(f'{k}×{v}' for k, v in dist.most_common())}")
