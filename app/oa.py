"""OA 采购订单拉取（**预留桩**，真 API 文档到位再实现）。

现状：OA 接口文档暂缺 → 本模块只搭好调用形状与字段映射位，**不发真请求、不假装成功**。
未配置 OA_BASE_URL/OA_TOKEN 时，一律返回 {"ok": False, "error": "OA 接口未配置（待对接）"}，
让前端明确提示"待对接，请先手工录入"，绝不静默造假数据。

真 API 到位后要做的只有两件事，其余链路（归集/落库/看板）都不用动：
  1. 在 fetch_orders()/fetch_order() 里发 HTTP 调用（带上 OA_TOKEN）。
  2. 在 _map_oa_to_order() 里把 OA 返回字段映射成本地订单字段。
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("yxq.oa")

# .env 已由 storage.db._load_dotenv() 在导入时加载到 os.environ，这里直接读即可。
_BASE_URL = (os.environ.get("OA_BASE_URL", "") or "").strip()
_TOKEN = (os.environ.get("OA_TOKEN", "") or "").strip()
# 演示模式：.env 设 OA_DEMO=1 → fetch_* 返回样例订单，方便真 API 到位前看整条链路效果。
_DEMO = (os.environ.get("OA_DEMO", "") or "").strip() in ("1", "true", "yes", "on")

_NOT_CONFIGURED = {"ok": False, "error": "OA 接口未配置（待对接）"}

# 演示样例：模拟 OA 返回的原始订单（字段名假设，真文档到了以 _map_oa_to_order 为准）
_DEMO_ORDERS = [
    {"orderNo": "PO-2026-0721", "customer": "深圳华芯电子", "supplier": "三星代理·港仓",
     "brand": "Samsung", "model": "M321R4GA3PB0-CWM", "capacity": "64GB",
     "frequency": "DDR5-5600", "cond": "全新", "source": "香港货",
     "qty": 200, "deliveryDate": "2026-08-05"},
    {"orderNo": "PO-2026-0722", "customer": "东莞恒久科技", "supplier": "海力士·马来仓",
     "brand": "SK Hynix", "model": "HMCG88MEBRA107N", "capacity": "32GB",
     "frequency": "DDR5-4800", "cond": "拆机", "source": "马来货",
     "qty": 150, "deliveryDate": "2026-07-30"},
    {"orderNo": "PO-2026-0723", "customer": "广州云仓", "supplier": "美光直采",
     "brand": "Micron", "model": "MTC20F2085S1RC48BA1", "capacity": "16GB",
     "frequency": "DDR5-4800", "cond": "拆新", "source": "国行",
     "qty": 300, "deliveryDate": "2026-08-12"},
]


def is_configured() -> bool:
    """OA_BASE_URL 与 OA_TOKEN 都填了才算配好（演示模式不算真配置）。"""
    return bool(_BASE_URL and _TOKEN)


def _map_oa_to_order(raw: dict) -> dict:
    """把 OA 单条订单原始返回 → 本地订单字段（create_batch 入参）。

    TODO(等 OA 文档)：按真实字段名补全右侧取值。预期本地需要的键：
      batch_no / oa_order_no / customer / supplier / brand / model /
      capacity / frequency / cond / remark(来源) / qty_expected / delivery_date
    现在先保留形状 + 透传原始返回（oa_raw），文档到了只改这里的映射。
    """
    return {
        "oa_order_no": str(raw.get("orderNo", "") or ""),
        "batch_no": str(raw.get("orderNo", "") or ""),
        "customer": str(raw.get("customer", "") or ""),
        "supplier": str(raw.get("supplier", "") or ""),
        "brand": str(raw.get("brand", "") or ""),
        "model": str(raw.get("model", "") or ""),
        "capacity": str(raw.get("capacity", "") or ""),
        "frequency": str(raw.get("frequency", "") or ""),
        "cond": str(raw.get("cond", "") or ""),
        "remark": str(raw.get("source", "") or ""),
        "qty_expected": int(raw.get("qty", 0) or 0),
        "delivery_date": str(raw.get("deliveryDate", "") or ""),
        "oa_synced": 1,
        "oa_raw": raw,
    }


def fetch_orders() -> dict:
    """拉取 OA 采购订单列表。

    返回 {"ok": True, "orders": [<本地订单字段dict>, ...]} 或 {"ok": False, "error": ...}。
    未配置 → 直接返回"待对接"，绝不造假。演示模式(OA_DEMO=1) → 返回样例订单。
    """
    if _DEMO:
        log.info("OA 演示模式：返回 %d 条样例订单", len(_DEMO_ORDERS))
        return {"ok": True, "orders": [_map_oa_to_order(o) for o in _DEMO_ORDERS]}
    if not is_configured():
        return dict(_NOT_CONFIGURED)
    # TODO(等 OA 文档): 用 _BASE_URL/_TOKEN 发 HTTP GET，遍历返回 → _map_oa_to_order()
    log.warning("OA 已配置但 fetch_orders 尚未实现真 HTTP 调用（待文档）")
    return {"ok": False, "error": "OA 拉取尚未实现（等接口文档）"}


def fetch_order(order_no: str) -> dict:
    """按订单号拉单条。返回 {"ok": True, "order": <dict>} 或 {"ok": False, "error": ...}。"""
    if not is_configured():
        return dict(_NOT_CONFIGURED)
    # TODO(等 OA 文档): 发 HTTP GET 单条 → _map_oa_to_order()
    log.warning("OA 已配置但 fetch_order 尚未实现真 HTTP 调用（待文档）")
    return {"ok": False, "error": "OA 拉取尚未实现（等接口文档）"}
