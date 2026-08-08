# -*- coding: utf-8 -*-
"""从一张"多根一盘"的照片**自动生成整图固定模板**（每颗自动框 + 按 x 聚成 N 根 + 写 slots）。

真机拍到一盘 4 根的正/反照片后，一条命令即出模板；之后 `analyze_and_save(mode=template)`
就能一次识别 N 根、拆 N 条入库。（识别/拆根/二维码/双相机链路都已就绪，独缺这份按真实取景的模板。）

用法：
  python tools/build_template.py <图片> --side front --id samsung-4up --brand Samsung --model M321R8GA0EB2 --sticks 4
  # 反面再跑一次（合并进同一模板）：
  python tools/build_template.py <反面图> --side back --id samsung-4up --sticks 4 --merge

说明：
- 用 recognize_rules 自动找日期框（颗粒 dram / PCB / 主控 controller），归一化后写入模板 boxes。
- 按各框 x 中心的最大间隙把它们聚成 N 根，生成 N 个 slots（每根一个矩形，纵向占满、供裁根+占位/标签判别）。
- 模板可管理：存到 template_store（app/recognition/templates/<id>.json）。之后可在此基础上人工微调。
"""
import argparse
import sys

from PIL import Image

from app.recognition.region_ocr import recognize_rules, _box_kind
from app.recognition import template_store


def _cluster_slots(items, n):
    """items=[(cx_norm, box_norm)]（cx 已归一化）→ 按最大 (n-1) 间隙聚成 n 组。

    返回 (slot_of(cx)->idx 函数, slots 矩形列表[x0,y0,x1,y1]归一化)。
    """
    xs = sorted(c for c, _ in items)
    if len(xs) < n:
        n = max(1, len(xs))
    gaps = sorted(((b - a, (a + b) / 2) for a, b in zip(xs, xs[1:])), reverse=True)[:n - 1]
    bounds = sorted(m for _, m in gaps)

    def slot_of(cx):
        return sum(1 for b in bounds if cx >= b)

    # 每组的 x 范围 → slot 矩形（纵向占满 0~1，x 各留少量余量）
    groups = {}
    for cx, box in items:
        groups.setdefault(slot_of(cx), []).append(box)
    slots = []
    for i in sorted(groups):
        allx = [p[0] for box in groups[i] for p in box]
        slots.append([max(0.0, min(allx) - 0.01), 0.0, min(1.0, max(allx) + 0.01), 1.0])
    slots.sort(key=lambda r: r[0])
    return slot_of, slots


def build(image_path, side, tid, brand, model, sticks, merge, current_year=None):
    img = Image.open(image_path).convert("RGB")
    W, H = img.size
    codes = recognize_rules(image_path, current_year=current_year)
    raw_keep = [c for c in codes if c.code_type in ("dram", "pcb", "controller") and c.box]
    if not raw_keep:
        raise SystemExit("没识别到任何日期框，换更清晰的图或先调好打光/对焦")

    # ---- 清洗：剔除“标签上被误当成日期”的框 ----
    # 建模板用整图 recognize_rules，会把白标签/型号丝印上的数字也框进来（如 "2010"→2020年10周、
    # 主控序列号片段 "1724..."→2017年24周）。这些若焊进模板，识别时就固定去读标签、污染判定。
    # 两道过滤：①落在白标签上的框（复用识别期同款 _box_kind 白标签判据）；
    #          ②低置信度的 pcb/controller 框（真颗粒 conf≈0.92，标签误检多为 0.6~0.7）。
    #          DRAM 一律保留（真颗粒；漏检的靠后续网格补全，不在此处删）。
    LABEL_CONF_MIN = 0.85
    keep, dropped = [], []
    for c in raw_keep:
        kind = _box_kind(img, c.box)          # c.box 是像素坐标（recognize_rules 输出）
        conf = getattr(c, "confidence", 1.0) or 1.0
        if kind == "label":
            dropped.append((c, "白标签")); continue
        if c.code_type in ("pcb", "controller") and conf < LABEL_CONF_MIN:
            dropped.append((c, f"低置信{conf:.2f}")); continue
        keep.append(c)
    if dropped:
        print(f"[清洗] 剔除 {len(dropped)} 个疑似标签误检：")
        for c, why in dropped:
            yw = f"{c.year}-{c.week}" if c.week else "RAW"
            print(f"  - {c.code_type} {yw} ({why}) src=\"{c.source_text}\"")
    if not keep:
        raise SystemExit("清洗后无有效框，检查打光/取景或放宽阈值")

    def norm(box):
        return [[round(p[0] / W, 4), round(p[1] / H, 4)] for p in box]

    dram = [c for c in keep if c.code_type == "dram"]
    items = []
    for c in dram:
        xs = [p[0] for p in c.box]
        items.append(((min(xs) + max(xs)) / 2 / W, norm(c.box)))
    slot_of, slots = _cluster_slots(items, sticks) if items else (lambda x: 0, [])

    boxes = []
    for i, c in enumerate(keep, 1):
        nb = norm(c.box)
        cx = sum(p[0] for p in nb) / len(nb)
        boxes.append({"type": c.code_type, "box": nb, "manual": False,
                      "id": i, "slot": slot_of(cx) if slots else -1})

    layout = {"image_size": [W, H], "boxes": boxes, "slots": slots}

    # 合并：若已存在该模板，保留另一面
    sides = {}
    if merge:
        old = template_store.get_template(tid)
        if old and old.get("sides"):
            sides = dict(old["sides"])
    sides[side] = layout

    meta = template_store.save_template(brand, model, f"自动生成({sticks}根){','.join(sides)}",
                                        sides, template_id=tid)
    n_dram = sum(1 for c in keep if c.code_type == "dram")
    n_pcb = sum(1 for c in keep if c.code_type == "pcb")
    print(f"已生成模板 {meta['id']}  面={side}  框={len(boxes)}(颗粒{n_dram}/PCB{n_pcb})  槽={len(slots)}")
    for i, s in enumerate(slots):
        print(f"  槽{i}: x[{s[0]:.3f},{s[2]:.3f}]  含颗粒 {sum(1 for b in boxes if b['type']=='dram' and b['slot']==i)}")
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--side", default="front", choices=["front", "back"])
    ap.add_argument("--id", required=True, help="模板 id")
    ap.add_argument("--brand", default="Samsung")
    ap.add_argument("--model", default="")
    ap.add_argument("--sticks", type=int, default=4)
    ap.add_argument("--merge", action="store_true", help="合并进已存在的同 id 模板(保留另一面)")
    ap.add_argument("--year", type=int, default=None)
    a = ap.parse_args()
    build(a.image, a.side, a.id, a.brand, a.model, a.sticks, a.merge, a.year)


if __name__ == "__main__":
    sys.exit(main())
