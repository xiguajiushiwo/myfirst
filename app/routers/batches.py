"""批次登记 + 良率统计（看板）接口。

批次登记：每批测试前登记 客户/品牌/容量/频率/批次号；之后该批每根质检归入并继承这些信息。
统计：今日/累计良率、按客户、按批次。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from .. import kingdee, oa
from ..storage import db

router = APIRouter()

_SYNC_INTERVAL = max(60, int(os.environ.get("KD_SYNC_INTERVAL", "900") or 900))
_INCREMENTAL_PAGES = max(1, int(os.environ.get("KD_INCREMENTAL_PAGES", "1") or 1))
_sync_lock = threading.Lock()
_sync_stop = threading.Event()
_sync_thread: threading.Thread | None = None
_SYNC_STATE_FILE = Path(os.environ.get(
    "KD_SYNC_STATE_FILE",
    str(Path(__file__).resolve().parents[2] / "config" / "kingdee_sync_state.json"),
))
_sync_state = {
    "running": False, "last_started": 0.0, "last_success": 0.0,
    "last_mode": "", "last_imported": 0,
    "last_error": "", "last_error_at": 0.0, "consecutive_failures": 0,
}


def _load_sync_state() -> None:
    try:
        saved = json.loads(_SYNC_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    for key in _sync_state:
        if key != "running" and key in saved:
            _sync_state[key] = saved[key]


def _save_sync_state() -> None:
    try:
        _SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        target = _SYNC_STATE_FILE.with_suffix(_SYNC_STATE_FILE.suffix + ".tmp")
        data = {**_sync_state, "running": False}
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        target.replace(_SYNC_STATE_FILE)
    except OSError:
        pass


_load_sync_state()

_KD_FIELDS = (
    "kd_bill_status", "kd_close_status", "kd_pay_mode", "kd_operator",
    "kd_supplier_number", "kd_supplier_status",
    "kd_material_number", "kd_src_bill_no", "kd_specification", "kd_total_amount",
    "kd_total_all_amount", "kd_tax_amount", "kd_received_qty",
    "kd_in_stock_qty", "kd_return_qty",
)


def _kd_kwargs(o: dict) -> dict:
    return {k: o.get(k, 0 if k.startswith(("kd_total", "kd_tax", "kd_received", "kd_in_stock", "kd_return")) else "")
            for k in _KD_FIELDS}


def _is_memory_order(o: dict) -> bool:
    text = " ".join(str(o.get(k) or "").lower() for k in (
        "model", "brand", "capacity", "frequency", "kd_specification",
        "kd_material_number", "remark",
    ))
    if any(x in text for x in (
        "中央处理器", "处理器", "cpu", "网卡", "network card", "nic",
        "显卡", "图形卡", "graphics card", "gpu", "geforce", "radeon", "rtx", "gtx",
    )):
        return False
    if any(x in text for x in ("内存", "ddr", "pc4", "pc5", "rdimm", "udimm", "sodimm")):
        return True
    return not o.get("oa_synced") and bool(o.get("capacity") or o.get("frequency"))


@router.post("/api/batches")
@router.post("/api/orders")            # 别名：新前端用"采购订单"术语，底层同一张 batches 表
def create_batch(
    batch_no: str = Form(""),
    customer: str = Form(""),
    brand: str = Form(""),
    capacity: str = Form(""),
    frequency: str = Form(""),
    cond: str = Form(""),
    remark: str = Form(""),
    model: str = Form(""),
    supplier: str = Form(""),
    kd_specification: str = Form(""),
    qty_expected: int = Form(0),
    delivery_date: str = Form(""),
    oa_order_no: str = Form(""),
):
    """登记一张采购订单（底层表仍叫 batch）。返回 {ok, id, batch}。"""
    if not (customer.strip() or batch_no.strip()):
        return JSONResponse({"ok": False, "error": "至少填客户或订单号"}, status_code=200)
    try:
        bid = db.create_batch(batch_no, customer, brand, capacity, frequency, cond, remark,
                              model=model, supplier=supplier, qty_expected=qty_expected,
                              delivery_date=delivery_date, oa_order_no=oa_order_no,
                              kd_specification=kd_specification)
        db.add_audit(customer, "create_batch", batch_no or str(bid),
                     f"{brand}/{model}/{kd_specification}/{supplier}/应检{qty_expected}")
        return {"ok": True, "id": bid, "batch": db.get_batch(bid)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/batches")
@router.get("/api/orders")
def list_batches(request: Request, limit: int = 100):
    try:
        batches = db.list_batches(limit)
        if request.url.path == "/api/orders":
            batches = [b for b in batches if _is_memory_order(b)]
        return {"batches": batches}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"batches": [], "error": str(e)}, status_code=200)


@router.get("/api/batches/{batch_id}")
@router.get("/api/orders/{batch_id}")
def get_batch(batch_id: int):
    b = None
    try:
        b = db.get_batch(batch_id)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
    if not b:
        return JSONResponse({"ok": False, "error": "订单不存在"}, status_code=404)
    return {"ok": True, "batch": b}


@router.post("/api/batches/{batch_id}/close")
@router.post("/api/orders/{batch_id}/close")
def close_batch(batch_id: int):
    try:
        ok = db.close_batch(batch_id)
        return {"ok": ok}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _import_orders(res: dict, source: str = "oa"):
    if not res.get("ok"):
        return JSONResponse(res, status_code=200)
    imported, ids = 0, []
    try:
        for o in res.get("orders", []):
            bid = db.create_batch(
                o.get("batch_no", ""), o.get("customer", ""), o.get("brand", ""),
                o.get("capacity", ""), o.get("frequency", ""), o.get("cond", ""),
                o.get("remark", ""), model=o.get("model", ""), supplier=o.get("supplier", ""),
                qty_expected=o.get("qty_expected", 0), delivery_date=o.get("delivery_date", ""),
                oa_order_no=o.get("oa_order_no", ""), oa_synced=1, oa_raw=o.get("oa_raw"),
                **_kd_kwargs(o))
            imported += 1
            ids.append(bid)
        return {"ok": True, "source": source, "imported": imported, "ids": ids,
                "total": res.get("total")}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _sync_kingdee(full: bool = False, force: bool = False) -> dict:
    """Run one guarded incremental sync.

    ``full`` is kept only for old callers. The system now always reads the newest
    Kingdee page(s), so a stale full-sync button or retry cannot pull every item
    back into local orders.
    """
    now = time.time()
    if not force and now - float(_sync_state["last_success"] or 0) < _SYNC_INTERVAL:
        return {"ok": True, "skipped": True, "reason": "cooldown", **sync_status()}
    if not _sync_lock.acquire(blocking=False):
        return {"ok": True, "skipped": True, "reason": "running", **sync_status()}
    mode = "incremental"
    _sync_state.update(running=True, last_started=now, last_mode=mode)
    try:
        pages = _INCREMENTAL_PAGES
        result = kingdee.fetch_orders(page_size=100, max_pages=pages, recent=True)
        imported = _import_orders(result, source="kingdee")
        payload = imported.body if isinstance(imported, JSONResponse) else None
        if payload is not None:
            import json
            imported = json.loads(payload)
        if not imported.get("ok"):
            raise RuntimeError(imported.get("error") or "金蝶同步失败")
        finished = time.time()
        _sync_state.update(
            last_success=finished,
            last_imported=int(imported.get("imported") or 0),
            last_error="",
            last_error_at=0.0,
            consecutive_failures=0,
        )
        _save_sync_state()
        return {**imported, "mode": mode, "pages": pages, "skipped": False}
    except Exception as exc:  # noqa: BLE001
        _sync_state.update(
            last_error=str(exc),
            last_error_at=time.time(),
            consecutive_failures=int(_sync_state.get("consecutive_failures") or 0) + 1,
        )
        _save_sync_state()
        return {"ok": False, "error": str(exc), "mode": mode}
    finally:
        _sync_state["running"] = False
        _sync_lock.release()


def sync_status() -> dict:
    return {
        "running": bool(_sync_state["running"]),
        "last_success": float(_sync_state["last_success"] or 0),
        "last_mode": _sync_state["last_mode"],
        "last_imported": int(_sync_state["last_imported"] or 0),
        "last_error": _sync_state["last_error"],
        "last_error_at": float(_sync_state["last_error_at"] or 0),
        "consecutive_failures": int(_sync_state["consecutive_failures"] or 0),
        "interval_seconds": _SYNC_INTERVAL,
        "configured": kingdee.is_configured(),
        "scheduler_running": bool(_sync_thread and _sync_thread.is_alive()),
    }


def _sync_loop() -> None:
    if _sync_stop.wait(10):
        return
    while not _sync_stop.is_set():
        _sync_kingdee()
        _sync_stop.wait(_SYNC_INTERVAL)


def start_order_sync() -> None:
    global _sync_thread
    if not kingdee.is_configured() or (_sync_thread and _sync_thread.is_alive()):
        return
    _sync_stop.clear()
    _sync_thread = threading.Thread(target=_sync_loop, daemon=True, name="kingdee-order-sync")
    _sync_thread.start()


def stop_order_sync() -> None:
    _sync_stop.set()
    if _sync_thread and _sync_thread.is_alive():
        _sync_thread.join(timeout=3)


def _import_one_order(res: dict, source: str = "kingdee"):
    if not res.get("ok"):
        return JSONResponse(res, status_code=200)
    o = res.get("order") or {}
    try:
        bid = db.create_batch(
            o.get("batch_no", ""), o.get("customer", ""), o.get("brand", ""),
            o.get("capacity", ""), o.get("frequency", ""), o.get("cond", ""),
            o.get("remark", ""), model=o.get("model", ""), supplier=o.get("supplier", ""),
            qty_expected=o.get("qty_expected", 0), delivery_date=o.get("delivery_date", ""),
            oa_order_no=o.get("oa_order_no", ""), oa_synced=1, oa_raw=o.get("oa_raw"),
            **_kd_kwargs(o))
        return {"ok": True, "source": source, "imported": 1, "ids": [bid], "id": bid,
                "batch": db.get_batch(bid)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/orders/sync_oa")
@router.post("/api/orders/sync_kingdee")
def sync_oa(billno: str = Form(""), full: bool = Form(False), force: bool = Form(False)):
    """从金蝶拉取采购订单并 upsert 进本地。

    保留 sync_oa 路径是为了兼容现有前端按钮；实际优先走金蝶 KD_* 配置。
    如果金蝶未配置，再回退 OA 预留模块，仍然明确返回错误，不造数据。
    """
    if kingdee.is_configured():
        if billno.strip():
            return _import_one_order(kingdee.fetch_order(billno), source="kingdee")
        return _sync_kingdee(force=force)
    return _import_orders(oa.fetch_orders(), source="oa")


@router.get("/api/order-sync/status")
def order_sync_status():
    return {"ok": True, **sync_status()}


@router.get("/api/stats/overview")
def stats_overview():
    """今日 / 累计 良率。"""
    try:
        return db.yield_overview()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=200)


@router.get("/api/stats/customers")
def stats_customers():
    """按客户汇总良率。"""
    try:
        return {"customers": db.customer_stats()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"customers": [], "error": str(e)}, status_code=200)
