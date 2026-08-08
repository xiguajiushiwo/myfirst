"""系统设置：多模态大模型（VL）配置 + 用量。

多 provider（可接多个模型）+ 当前启用；用量取 metrics（调用次数/token，费用按单价估算）。
"""
from __future__ import annotations

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse

from .. import metrics, settings_store
from ..storage import db

router = APIRouter()


@router.get("/api/settings/vl")
def get_vl():
    """当前 VL 配置（api_key 打码）+ 用量统计。"""
    cfg = settings_store.get_config(masked=True)
    usage = metrics.vl_usage()
    # 费用估算：按当前 provider 的 price_per_1k（元/1k token）
    active = next((p for p in cfg["providers"] if p["id"] == cfg["active"]), None)
    price = float((active or {}).get("price_per_1k", 0) or 0)
    usage["cost_est"] = round(usage["total_tokens"] / 1000 * price, 2) if price else None
    return {"ok": True, "config": cfg, "usage": usage}


@router.post("/api/settings/vl/provider")
def upsert_provider(
    id: str = Form(""),
    name: str = Form(""),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),           # 留空=保留原 key
    timeout: int = Form(120),
    price_per_1k: float = Form(0.0),
):
    """新增/更新一个多模态模型 provider。"""
    if not (name.strip() or id.strip()):
        return JSONResponse({"ok": False, "error": "请填模型名称"}, status_code=200)
    p = settings_store.upsert_provider({
        "id": id.strip(), "name": name.strip(), "base_url": base_url.strip(),
        "model": model.strip(), "api_key": api_key.strip(),
        "timeout": int(timeout or 120), "price_per_1k": float(price_per_1k or 0),
    })
    try:
        db.add_audit("", "vl_provider", p.get("id", ""), f"{p.get('name')}/{p.get('model')}")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "provider": {**p, "api_key": settings_store._mask(p.get("api_key", ""))}}


@router.post("/api/settings/vl/active")
def set_active(id: str = Form(...)):
    ok = settings_store.set_active(id)
    return {"ok": ok} if ok else JSONResponse({"ok": False, "error": "无此模型"}, status_code=200)


@router.delete("/api/settings/vl/provider/{pid}")
def delete_provider(pid: str):
    ok = settings_store.delete_provider(pid)
    return {"ok": ok} if ok else JSONResponse(
        {"ok": False, "error": "删除失败（不存在或不能删到空）"}, status_code=200)
