"""从照片解码内存条**标签上的大 DataMatrix 码**，精确拿到 SN / 型号 / 规格。

只取标签大码（文本含 `(S)/(P)/(L)` 等结构化字段）；**忽略芯片上的小数字码**（如 1478...）。
解码是带纠错的确定性结果，比 OCR 猜 SN 可靠得多——SN 是追溯/拆记录/去重的主键。
用 zxing-cpp（纯 pip 包，认 DataMatrix，无需外部 DLL）。
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("yxq.barcode")

# 标签码形如：(L)规格(S)序列号(P)型号(M)批次码
_FIELD_RE = re.compile(r"\(([A-Z])\)([^()]*)")


def brand_from_model(model: str) -> str:
    """从型号(P字段)前缀推品牌。标签码不直接带品牌，但型号前缀是业界标准编法。"""
    m = (model or "").upper().strip()
    if not m:
        return ""
    # 顺序敏感：MT(美光) 必须在 M(三星) 之前判
    for pre, brand in (
        ("MT", "Micron"), ("HM", "SK Hynix"), ("CT", "Crucial"),
        ("KVR", "Kingston"), ("KSM", "Kingston"), ("KF", "Kingston"), ("KHX", "Kingston"),
        ("CM", "Corsair"), ("F4-", "G.Skill"), ("F5-", "G.Skill"), ("NT", "Nanya"),
    ):
        if m.startswith(pre):
            return brand
    if m[0] == "M" and m[1:2].isdigit():        # M3xx/M4xx… = 三星 DRAM 模组
        return "Samsung"
    return ""


def parse_label_text(text: str) -> dict:
    """解析标签码文本 → {sn, model, brand, spec, mfg, capacity, frequency, raw}。

    (S)=序列号 (P)=型号 (L)=规格 (M)=批次码；并从规格抽容量/频率、从型号推品牌。
    非结构化（不含这些字段）时 sn 为空。
    """
    d = dict(_FIELD_RE.findall(text or ""))
    spec = (d.get("L") or "").strip()
    cap = ""
    m = re.search(r"\d+\s*GB", spec, re.I)
    if m:
        cap = m.group(0).replace(" ", "").upper()
    freq = ""
    m = re.search(r"PC(\d)-(\d+)", spec, re.I)          # PC5-5600 → DDR5-5600
    if m:
        freq = f"DDR{m.group(1)}-{m.group(2)}"
    model = (d.get("P") or "").strip()
    return {
        "sn": (d.get("S") or "").strip(),
        "model": model,
        "brand": brand_from_model(model),
        "spec": spec,
        "mfg": (d.get("M") or "").strip(),
        "capacity": cap,
        "frequency": freq,
        "raw": text or "",
        "src": "barcode",
    }


def _is_label_code(text: str) -> bool:
    """是不是标签上的**大码**：含 (S) 序列号字段（芯片小码是纯数字、不含这些）。"""
    return "(S)" in (text or "")


def decode_labels(image) -> list[dict]:
    """解码一张图里所有**标签大码**（忽略芯片小码）。

    image 可为 文件路径 / numpy 数组 / PIL 图。返回 [{...parse..., pos_cx}]，按 x 从左到右排。
    """
    try:
        import cv2
        import zxingcpp
    except Exception as e:  # noqa: BLE001
        log.warning("解码库不可用：%s", e)
        return []
    try:
        im = cv2.imread(image) if isinstance(image, str) else image
        if im is None:
            return []
        out = []
        for r in zxingcpp.read_barcodes(im):
            if not _is_label_code(r.text):            # 只要标签大码，芯片小数字码跳过
                continue
            info = parse_label_text(r.text)
            try:
                p = r.position
                info["pos_cx"] = (p.top_left.x + p.bottom_right.x) / 2
            except Exception:
                info["pos_cx"] = 0
            out.append(info)
        out.sort(key=lambda d: d["pos_cx"])
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("标签码解码失败：%s", e)
        return []


def read_label_code(image) -> dict:
    """从一根/一个标签的图里取一个标签大码；解不出返回 {}。"""
    codes = decode_labels(image)
    return codes[0] if codes else {}
