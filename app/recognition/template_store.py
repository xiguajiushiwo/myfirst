"""内存条型号模板库（按品牌+型号管理固定框坐标）。

每个模板 = `app/templates/<id>.json`，结构：
  {
    "id", "brand", "model", "note", "created",
    "sides": { "front": {"image_size":[W,H], "boxes":[{type,box,manual,id}]},
               "back":  {...} }
  }

- 不维护单独索引文件：`list_templates()` 直接扫描目录。
  → 将来用 PaddleOCR 批量自动框生成的 JSON，只要丢进本目录即自动生效。
- 也暴露 `save_template()` 供将来的批量生成脚本调用。

识别时按 `template_id` 取该型号的框坐标套用（见 region_ocr.recognize_side）。
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import Counter
from typing import Optional

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
DEFAULT_TEMPLATE_ID = "samsung-4up-0808"

_lock = threading.Lock()
_cache: dict[str, dict] = {}


def _ensure_dir():
    os.makedirs(TEMPLATES_DIR, exist_ok=True)


def _path(template_id: str) -> str:
    return os.path.join(TEMPLATES_DIR, f"{template_id}.json")


def _slugify(text: str) -> str:
    """品牌+型号 → 文件名安全的 id。"""
    s = re.sub(r"[^0-9a-zA-Z]+", "-", (text or "").lower()).strip("-")
    return s or "tpl"


def _counts(tpl: dict) -> dict:
    """统计该模板各面/各类型框数，供前端列表展示。"""
    out: dict = {}
    for side, layout in (tpl.get("sides") or {}).items():
        boxes = layout.get("boxes", [])
        by_type = dict(Counter(b.get("type", "?") for b in boxes))
        out[side] = {"total": len(boxes), **by_type}
    return out


def _read(template_id: str) -> Optional[dict]:
    p = _path(template_id)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def list_templates() -> list[dict]:
    """扫描模板目录，返回各模板元信息（不含完整 boxes）。"""
    _ensure_dir()
    items = []
    for fn in os.listdir(TEMPLATES_DIR):
        if not fn.endswith(".json"):
            continue
        tid = fn[:-5]
        try:
            tpl = _read(tid)
        except Exception:
            continue
        if not tpl:
            continue
        if tpl.get("retired") is True:
            continue
        items.append({
            "id": tpl.get("id", tid),
            "brand": tpl.get("brand", ""),
            "model": tpl.get("model", ""),
            "note": tpl.get("note", ""),
            "created": tpl.get("created", ""),
            "calibrated": tpl.get("calibrated", True) is not False,
            "capacity": tpl.get("capacity", ""),
            "frequency": tpl.get("frequency", ""),
            "requirements": tpl.get("requirements", []),
            "counts": _counts(tpl),
        })
    items.sort(key=lambda x: (x["brand"], x["model"], x["id"]))
    return items


def get_template(template_id: str) -> Optional[dict]:
    """取完整模板（带缓存）。"""
    if not template_id:
        return None
    with _lock:
        if template_id in _cache:
            return _cache[template_id]
        tpl = _read(template_id)
        if tpl is not None:
            _cache[template_id] = tpl
        return tpl


def default_template_id() -> Optional[str]:
    """识别时未指定模板时的兜底（只取已标定模板）。"""
    default = get_template(DEFAULT_TEMPLATE_ID)
    if default and default.get("calibrated", True) is not False:
        return DEFAULT_TEMPLATE_ID
    items = [item for item in list_templates() if item.get("calibrated")]
    return items[0]["id"] if items else None


def is_calibrated(template_id: str) -> bool:
    tpl = get_template(template_id)
    return bool(tpl and tpl.get("calibrated", True) is not False)


def delete_template(template_id: str) -> bool:
    p = _path(template_id)
    if os.path.exists(p):
        os.remove(p)
        with _lock:
            _cache.pop(template_id, None)
        return True
    return False


def save_template(brand: str, model: str, note: str, sides: dict,
                  template_id: Optional[str] = None, created: str = "") -> dict:
    """写入/覆盖一个模板，返回其元信息。

    供将来的「PaddleOCR 批量自动框 → 生成模板」脚本调用；本轮前端不使用。
    """
    _ensure_dir()
    tid = template_id or _slugify(f"{brand}-{model}")
    tpl = {
        "id": tid, "brand": brand, "model": model,
        "note": note or "", "created": created or "", "sides": sides,
    }
    with open(_path(tid), "w", encoding="utf-8") as f:
        json.dump(tpl, f, ensure_ascii=False, indent=2)
    with _lock:
        _cache[tid] = tpl
    return {"id": tid, "brand": brand, "model": model,
            "note": tpl["note"], "created": tpl["created"], "counts": _counts(tpl)}
