"""托盘槽位占位阈值标定：打印每个槽的 presence 分数，帮你把 SLOT_PRESENCE_MIN 设到合适值。

用法：
  # 满盘(每槽都放条)拍一张、空盘(全空或部分空)拍一张，分别跑：
  .venv/Scripts/python.exe tools/calibrate_slots.py <模板id> <side:front|back> <图片路径>

看输出：有条的槽 score 应明显高、空槽应接近 0；把 SLOT_PRESENCE_MIN（.env 或环境变量）
设在两者中间（如满盘最低 0.08、空槽最高 0.01 → 设 0.03~0.05）。

模板若没写显式 `slots`，会按框 x 跨度自动聚类兜底（双列/紧排易误切，建议真机模板写显式 slots）。
"""
from __future__ import annotations

import sys

from PIL import Image

from app.recognition import template_store
from app.recognition.region_ocr import (_slot_rects_for_layout,
                                         _slot_axis,
                                         detect_occupied_slots,
                                         _SLOT_PRESENCE_MIN)


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 1
    tid, side, img_path = argv[0], argv[1], argv[2]
    tpl = template_store.get_template(tid)
    if not tpl:
        print(f"未找到模板：{tid!r}")
        return 1
    layout = (tpl.get("sides") or {}).get(side)
    if not layout:
        print(f"模板 {tid} 不含「{side}」面")
        return 1

    rects = _slot_rects_for_layout(layout)
    explicit = bool(layout.get("slots"))
    img = Image.open(img_path).convert("RGB")
    occ = detect_occupied_slots(img, rects, axis=_slot_axis(layout))

    print(f"模板={tid} side={side} 图={img_path} 尺寸={img.size}")
    print(f"槽来源={'显式 slots' if explicit else '自动聚类兜底'}  当前阈值 SLOT_PRESENCE_MIN={_SLOT_PRESENCE_MIN}")
    print(f"检测到 {len(occ)} 个槽：")
    for o in occ:
        flag = "有条" if o["occupied"] else "空位"
        print(f"  槽{o['slot']}: score={o['score']:.4f}  → {flag}   (px box={o['box']})")
    occ_n = sum(1 for o in occ if o["occupied"])
    print(f"判定：本盘 {occ_n} 根有条 / 共 {len(occ)} 槽")
    if occ:
        lo = min(o["score"] for o in occ)
        print(f"提示：满盘时把阈值设在 <{lo:.4f}；空盘时把阈值设在 >空槽最高分。取两者中间最稳。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
