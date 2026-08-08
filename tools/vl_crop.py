# -*- coding: utf-8 -*-
"""按 vl_locate.py 的框坐标裁块存盘，供人工复核 + 交多模态大模型。

分两类裁法，因为两类目标的失败模式不同：
  颗粒(dram)  —— 字大、正印，只需按框加边距裁出来，2× 放大够了。
  pmic/sot/pcb —— 字小、竖排或倒印。本会话实测：PMIC 竖排必须转 270° 才读得出、
                  过度放大反而掉字（3×/4× 把 MPS2531 读成 MPS253）。所以固定 2×。
                  PCB 一盘之内朝向不统一（bmp 槽1/2 正、槽3/4 倒），
                  **不能硬编码朝向** —— 正倒两张都存，交给大模型自己认。

用法：
    .venv\\Scripts\\python.exe tools\\vl_crop.py logs\\_vl\\<stem>_regions.json
产物：
    logs/_vl/crops/<stem>/s<槽>_dram<编号>.png
    logs/_vl/crops/<stem>/s<槽>_{pmic,sot}.png            （已转 270°）
    logs/_vl/crops/<stem>/s<槽>_pcb_{r0,r180}.png         （正/倒两张）
    logs/_vl/crops/<stem>/index.json                      （块清单，供 vl_read.py 用）
"""
import json
import os
import sys

from PIL import Image

REG = sys.argv[1] if len(sys.argv) > 1 else None
if not REG:
    sys.exit("用法: vl_crop.py logs/_vl/<stem>_regions.json")

with open(REG, encoding="utf-8") as f:
    reg = json.load(f)

img = Image.open(reg["src"]).convert("RGB")
W, H = img.size
stem = os.path.splitext(os.path.basename(reg["src"]))[0]
OUT = f"logs/_vl/crops/{stem}"
os.makedirs(OUT, exist_ok=True)

UP_DRAM = 2          # 颗粒放大倍数
UP_CHIP = 2          # pmic/sot/pcb 放大倍数（实测 >2 反而掉字）
# 颗粒框外扩：y 给得比 x 大得多。行投影的边界会压在最上那行字上
# （实测 s2_dram05 把日期行 'SEC 546' 切到框外了），而日期恰在首行。
PAD_X, PAD_Y = 0.03, 0.12


def cut(box, padx=0.0, pady=0.0):
    x0, y0, x1, y1 = box
    dx, dy = int((x1 - x0) * padx), int((y1 - y0) * pady)
    return img.crop((max(0, x0 - dx), max(0, y0 - dy), min(W, x1 + dx), min(H, y1 + dy)))


def up(im, k):
    return im.resize((im.width * k, im.height * k), Image.LANCZOS)


index = {"src": reg["src"], "blocks": []}

for s in reg["slots"]:
    si = s["slot"]
    for p in s["particles"]:
        f = f"s{si}_dram{p['i']:02d}.png"
        up(cut(p["box"], PAD_X, PAD_Y), UP_DRAM).save(f"{OUT}/{f}")
        index["blocks"].append({"file": f, "slot": si, "kind": "dram",
                                "idx": p["i"], "box": p["box"], "rot": 0})

    # PMIC：竖排丝印，转 270° 让字立起来（mid_rot_probe 实测四根全中）
    box = s["fixed"]["pmic"]
    f = f"s{si}_pmic.png"
    up(cut(box), UP_CHIP).rotate(-270, expand=True).save(f"{OUT}/{f}")
    index["blocks"].append({"file": f, "slot": si, "kind": "pmic",
                            "idx": 0, "box": box, "rot": 270})

    # SOT：三行倒排、行高仅 12~13px，是全流程最难的一处。
    # 给它更大的倍数（OCR 下 >2× 会掉字，但大模型没有 det 切行的问题，
    # 放大只是让笔画更实），并且和 PMIC 一样转 270°。
    box = s["fixed"]["sot"]
    f = f"s{si}_sot.png"
    # 注意朝向与 PMIC 相反：这颗的丝印是倒排的，转 90° 才立正
    up(cut(box, 0.05, 0.20), 6).rotate(-90, expand=True).save(f"{OUT}/{f}")
    index["blocks"].append({"file": f, "slot": si, "kind": "sot",
                            "idx": 0, "box": box, "rot": 270})

    # PCB：朝向一盘之内不统一，正/倒各存一张
    box = s["fixed"]["pcb"]
    base = up(cut(box), UP_CHIP)
    for rot, suf in ((0, "r0"), (180, "r180")):
        f = f"s{si}_pcb_{suf}.png"
        (base if rot == 0 else base.rotate(180)).save(f"{OUT}/{f}")
        index["blocks"].append({"file": f, "slot": si, "kind": "pcb",
                                "idx": 0, "box": box, "rot": rot})

with open(f"{OUT}/index.json", "w", encoding="utf-8") as f:
    json.dump(index, f, ensure_ascii=False, indent=1)

kinds = {}
for b in index["blocks"]:
    kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
print(f"裁块 {len(index['blocks'])} 张 → {OUT}/")
for k, v in kinds.items():
    print(f"  {k:5s} {v}")
