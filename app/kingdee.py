"""金蝶云星瀚（Cosmic/苍穹）kapi 只读对接：拿 OAuth token → 拉采购订单 → 映射本地订单字段。

**只读**：仅调用查询类接口，绝不写/改金蝶数据。

鉴权已实测通（2026-07-28）：
  POST {KD_BASE_URL}/kapi/oauth2/getToken
  header: x-acgw-identity: <KD_IDENTITY>
  body(JSON): grant_type=client_credentials, client_id, client_secret,
              username, password(=client_secret), nonce(一次性随机), timestamp(13位ms)
  → data.access_token（Bearer，expires_in≈7200s）

业务查询接口路径 KD_ORDER_API 需金蝶提供（各租户/业务云不同，无法猜）。
未配置 KD_* 或未配 KD_ORDER_API 时，fetch_* 返回明确错误，不造假。

凭证只从环境（.env，已 gitignore）读，绝不写死。
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import os
import re
import time
import uuid

import requests

log = logging.getLogger("yxq.kingdee")

_BASE = (os.environ.get("KD_BASE_URL", "") or "").strip().rstrip("/")
_CID = (os.environ.get("KD_CLIENT_ID", "") or "").strip()
_SECRET = (os.environ.get("KD_CLIENT_SECRET", "") or "").strip()
_USER = (os.environ.get("KD_USERNAME", "") or "").strip()
_IDENTITY = (os.environ.get("KD_IDENTITY", "") or "").strip()
_ACCT = (os.environ.get("KD_ACCOUNT_ID", "") or "").strip()
# 采购订单查询接口（文档确认路径；可用 .env KD_ORDER_API 覆盖，如换租户版 /kapi/v2/yk70/pm/...）
_ORDER_API = (os.environ.get("KD_ORDER_API", "") or "").strip() or "/kapi/v2/pm/pm_purorderbill/query"
_SUPPLIER_API = (os.environ.get("KD_SUPPLIER_API", "") or "").strip() or "/kapi/v2/yk70/basedata/bd_supplier/query"
_SALES_ORDER_API = (os.environ.get("KD_SALES_ORDER_API", "") or "").strip() or "/kapi/v2/sm/sm_salorder/query"

_TIMEOUT = int(os.environ.get("KD_TIMEOUT", "30") or 30)

# access_token 内存缓存（有效期内复用，避免每次都换 token）
_token: dict = {"value": "", "exp": 0.0}


def _token_lifetime_seconds(raw) -> float:
    """兼容金蝶不同网关：expires_in 可能返回秒，也可能返回毫秒。"""
    try:
        value = float(raw or 7200)
    except (TypeError, ValueError):
        value = 7200.0
    if value > 86400:
        value /= 1000.0
    return max(60.0, value)


def is_configured() -> bool:
    """鉴权四件套齐全才算配好（业务查询接口另判）。"""
    return bool(_BASE and _CID and _SECRET and _USER and _IDENTITY)


def get_token(force: bool = False) -> str:
    """取 access_token（缓存复用；过期或 force 时重新获取）。失败抛 RuntimeError。"""
    now = time.time()
    if not force and _token["value"] and now < _token["exp"] - 60:
        return _token["value"]
    if not is_configured():
        raise RuntimeError("金蝶未配置（缺 KD_BASE_URL/CLIENT_ID/CLIENT_SECRET/USERNAME/IDENTITY）")
    body = {
        "grant_type": "client_credentials",
        "client_id": _CID, "client_secret": _SECRET,
        "username": _USER, "password": _SECRET,
        "nonce": uuid.uuid4().hex, "timestamp": str(int(now * 1000)),
    }
    r = requests.post(f"{_BASE}/kapi/oauth2/getToken", json=body,
                      headers={"Content-Type": "application/json", "x-acgw-identity": _IDENTITY},
                      timeout=_TIMEOUT)
    js = r.json()
    if not js.get("status") or not (js.get("data") or {}).get("access_token"):
        raise RuntimeError(f"getToken 失败: {js.get('message') or js}")
    data = js["data"]
    lifetime = _token_lifetime_seconds(data.get("expires_in"))
    _token["value"] = data["access_token"]
    _token["exp"] = now + lifetime
    log.info("金蝶 token 已获取（有效期约 %.0fs）", lifetime)
    return _token["value"]


def _auth_headers() -> dict:
    """业务查询请求头：token（多种兼容写法）+ identity + accountId。"""
    tok = get_token()
    return {
        "Content-Type": "application/json",
        "accessToken": tok,
        "accesstoken": tok,
        "access_token": tok,
        "Authorization": f"Bearer {tok}",
        "x-acgw-identity": _IDENTITY,
        "Idempotency-Key": uuid.uuid4().hex,
        "accountId": _ACCT,
    }


def _token_auth_failed(message) -> bool:
    text = str(message or "").lower()
    return "token" in text and any(word in text for word in ("过期", "expired", "认证不通过", "invalid"))


def _post_query(path: str, body: dict, error_label: str) -> dict:
    """执行金蝶只读查询；token 失效时刷新并原请求重试一次。"""
    for attempt in range(2):
        response = requests.post(f"{_BASE}{path}", json=body,
                                 headers=_auth_headers(), timeout=_TIMEOUT)
        payload = response.json()
        if payload.get("status"):
            return payload
        message = payload.get("message") or payload
        if attempt == 0 and _token_auth_failed(message):
            get_token(force=True)
            continue
        raise RuntimeError(f"{error_label}: {message}")
    raise RuntimeError(f"{error_label}: token 刷新后仍认证失败")


def query_orders(filter_str: str = "1 = 1", page_no: int = 1, page_size: int = 50,
                 billno: str = "") -> dict:
    """调采购订单查询接口，返回原始 {rows:[...], totalCount:n}。
    filter_str: 金蝶 SQL 式过滤，如 "billstatus = 'C'"、"billno = 'CGDD-260505-000001'"。"""
    data = {"billno": billno} if billno else {"filter": f"[{filter_str}]"}
    body = {"data": data, "pageNo": int(page_no), "pageSize": int(page_size)}
    js = _post_query(_ORDER_API, body, "采购订单查询失败")
    data = js.get("data") or {}
    return {
        "rows": data.get("rows") or [],
        "totalCount": data.get("totalCount") or data.get("total") or 0,
        "lastPage": data.get("lastPage"),
        "pageNo": data.get("pageNo") or page_no,
        "pageSize": data.get("pageSize") or page_size,
    }


def query_suppliers(numbers: list[str], page_no: int = 1, page_size: int = 100) -> dict:
    """按供应商编码查询金蝶供应商基础资料，返回原始 rows。"""
    nums = [str(n).strip() for n in numbers if str(n or "").strip()]
    if not nums:
        return {"rows": [], "totalCount": 0, "lastPage": True}
    body = {"data": {"number": nums}, "pageNo": int(page_no), "pageSize": int(page_size)}
    js = _post_query(_SUPPLIER_API, body, "供应商查询失败")
    data = js.get("data") or {}
    return {
        "rows": data.get("rows") or [],
        "totalCount": data.get("totalCount") or data.get("total") or 0,
        "lastPage": data.get("lastPage"),
        "pageNo": data.get("pageNo") or page_no,
        "pageSize": data.get("pageSize") or page_size,
    }


def query_sales_orders(billnos: list[str] | str, page_no: int = 1, page_size: int = 100) -> dict:
    nums = [billnos] if isinstance(billnos, str) else billnos
    unique = []
    seen = set()
    for n in nums:
        s = str(n or "").strip()
        if s and s not in seen:
            seen.add(s)
            unique.append(s)
    if not unique:
        return {"rows": [], "totalCount": 0, "lastPage": True}
    body_billno = unique[0] if len(unique) == 1 else unique
    body = {"data": {"billno": body_billno}, "pageNo": int(page_no), "pageSize": int(page_size)}
    js = _post_query(_SALES_ORDER_API, body, "销售订单查询失败")
    data = js.get("data") or {}
    return {
        "rows": data.get("rows") or [],
        "totalCount": data.get("totalCount") or data.get("total") or 0,
        "lastPage": data.get("lastPage"),
        "pageNo": data.get("pageNo") or page_no,
        "pageSize": data.get("pageSize") or page_size,
    }


def supplier_name_map(numbers: list[str], chunk_size: int = 100) -> dict[str, dict]:
    """供应商编码 -> {name, status}。查询失败时让上层决定是否整体失败。"""
    unique = []
    seen = set()
    for n in numbers:
        s = str(n or "").strip()
        if s and s not in seen:
            seen.add(s)
            unique.append(s)
    out: dict[str, dict] = {}
    for i in range(0, len(unique), max(1, int(chunk_size))):
        chunk = unique[i:i + max(1, int(chunk_size))]
        res = query_suppliers(chunk, page_no=1, page_size=max(len(chunk), 10))
        for row in res.get("rows") or []:
            if not isinstance(row, dict):
                continue
            number = _first(row, "number", "suppliernumber", "supplier_number")
            if not number:
                continue
            out[number] = {
                "name": _first(row, "name", "fullname") or number,
                "status": _first(row, "supplier_status_name", "status", "enable"),
            }
    return out


def sales_spec_map(billnos: list[str], chunk_size: int = 50) -> dict[tuple[str, str], str]:
    """Map (sales bill no, material number) to Kingdee material model spec."""
    unique = []
    seen = set()
    for n in billnos:
        s = str(n or "").strip()
        if s and s not in seen:
            seen.add(s)
            unique.append(s)
    out: dict[tuple[str, str], str] = {}
    for i in range(0, len(unique), max(1, int(chunk_size))):
        chunk = unique[i:i + max(1, int(chunk_size))]
        res = query_sales_orders(chunk, page_no=1, page_size=max(len(chunk), 10))
        for row in res.get("rows") or []:
            if not isinstance(row, dict):
                continue
            billno = _first(row, "billno", "billNo")
            for entry in _row_entries(row):
                material_no = _first(entry, "material_masterid_number", "materialnumber",
                                     "material_number", "materialid")
                spec = _first(entry, "material_masterid_modelnum", "material_modelnum",
                              "modelnum", "specification")
                if billno and material_no and spec:
                    out[(billno, material_no)] = spec
    return out


def sales_material_spec_map(page_size: int = 100, max_pages: int = 20) -> dict[str, str]:
    """Map material number to spec from sales orders as a fallback for purchase rows without source bill no."""
    out: dict[str, str] = {}
    page_no = 1
    while page_no <= max_pages:
        res = query_sales_orders("*", page_no=page_no, page_size=page_size)
        rows = res.get("rows") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for entry in _row_entries(row):
                material_no = _first(entry, "material_masterid_number", "materialnumber",
                                     "material_number", "materialid")
                spec = _first(entry, "material_masterid_modelnum", "material_modelnum",
                              "modelnum", "specification")
                if material_no and spec and material_no not in out:
                    out[material_no] = spec
        if not rows or res.get("lastPage") is True:
            break
        total = int(res.get("totalCount") or 0)
        if total and page_no * page_size >= total:
            break
        page_no += 1
    return out


def _num(v) -> float:
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return 0.0


def _money(v) -> float:
    return round(_num(v), 2)


def _join_unique(values: list[str], sep: str = "、") -> str:
    out, seen = [], set()
    for v in values:
        s = (v or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return sep.join(out)


def _text(v, *keys: str) -> str:
    """金蝶基础资料字段常见形态：字符串、数字、{name/number/...}、多语言数组。"""
    if v is None:
        return ""
    if isinstance(v, (str, int, float)):
        return str(v).strip()
    if isinstance(v, list):
        for item in v:
            s = _text(item, *keys)
            if s:
                return s
        return ""
    if isinstance(v, dict):
        for k in (*keys, "name", "fullname", "number", "billno", "id"):
            if k in v:
                s = _text(v.get(k), *keys)
                if s:
                    return s
    return ""


def _first(row: dict, *keys: str) -> str:
    for k in keys:
        if k in row:
            s = _text(row.get(k))
            if s:
                return s
    return ""


def _date(v) -> str:
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.strftime("%Y-%m-%d")
    s = _text(v)
    return s[:10] if s else ""


def _parse_memory_spec(spec: str) -> tuple[str, str]:
    s = str(spec or "").strip()
    if not s:
        return "", ""
    caps = [f"{m}GB" for m in re.findall(r"(?<!\d)(\d{1,4})\s*(?:GB|G)\b", s, re.I)]
    memory_hint = bool(caps) or bool(re.search(r"\b(?:DDR[345]|PC[345]|R?DIMM|UDIMM|SODIMM)\b", s, re.I))
    freqs = (re.findall(r"\b(?:DDR[345]\s*[- ]?|PC[345]\s*[- ]?)?(\d{4,5})(?:\s*(?:MT/s|MHz))?\b", s, re.I)
             if memory_hint else [])
    return _join_unique(caps), _join_unique(freqs)


def _entry_material(entry: dict) -> str:
    for k in ("materialname", "material_name", "material", "materialid", "materialId",
              "materialmasterid", "material_number", "materialnumber"):
        if k in entry:
            s = _text(entry.get(k), "name", "number")
            if s:
                return s
    return ""


def _entry_spec(entry: dict) -> str:
    for k in ("material_masterid_modelnum", "entry_material_masterid_modelnum",
              "material_modelnum", "entry_material_modelnum", "modelnum",
              "entry_modelnum", "specification", "entry_specification"):
        if k in entry:
            s = _text(entry.get(k))
            if s:
                return s
    return ""


def _row_entries(row: dict) -> list[dict]:
    entries = row.get("billentry") or row.get("billEntry") or row.get("entries") or []
    if isinstance(entries, dict):
        return [entries]
    if isinstance(entries, list):
        return [e for e in entries if isinstance(e, dict)]
    return []


def _flatten_orders(rows: list[dict]) -> list[dict]:
    """兼容两种返回：单据头+billentry，或每条分录行平铺。"""
    flattened = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        entries = _row_entries(row)
        if not entries:
            flattened.append(dict(row))
            continue
        for entry in entries:
            merged = dict(row)
            merged.pop("billentry", None)
            merged.pop("billEntry", None)
            merged.pop("entries", None)
            for k, v in entry.items():
                merged.setdefault(k, v)
                merged[f"entry_{k}"] = v
            flattened.append(merged)
    return flattened


def _map_order(billno: str, rows: list[dict]) -> dict:
    """把同一采购单的单据头/分录 → 一条本地订单字段（create_batch 入参）。

    字段映射（据「云小圈集成字段映射」文档，采购订单 pm_purorderbill）：
      billno       → batch_no / oa_order_no（单据编号）
      suppliername → supplier（供应商名称）
      invqty 累加  → qty_expected（应检数量，按已入库数量）
      materialname → model（物料名称，多个用、拼接）
      biztime      → delivery_date（订单/业务日期）
    采购单本身没有 客户/品牌/容量/频率 → 留空待人工补（或后续接物料基础资料）。
    整单原始返回存 oa_raw 备查。
    """
    head = rows[0] if rows else {}
    supplier_name = _first(head, "suppliername", "supplier")
    supplier_number = _first(head, "providersuppliernumber", "suppliernumber",
                             "supplier_number", "supplierid", "supplierId")
    supplier = supplier_name or supplier_number
    biztime = _date(head.get("biztime") or head.get("billdate") or head.get("date") or head.get("createtime"))
    qty = sum(_num(r.get("qty") or r.get("entry_qty") or r.get("baseqty") or r.get("entry_baseqty")) for r in rows)
    materials, seen = [], set()
    material_numbers, src_bill_nos, specs = [], [], []
    for r in rows:
        m = (_entry_material(r) or _first(r, "entry_materialname", "entry_material_number",
                                         "entry_materialnumber", "materialname",
                                         "material_number", "materialnumber"))
        if m and m not in seen:
            seen.add(m)
            materials.append(m)
        material_numbers.append(_first(r, "entry_materialnumber", "materialnumber"))
        src_bill_nos.append(_first(r, "entry_srcbillnumber", "srcbillnumber",
                                   "entry_salbillnumber", "salbillnumber"))
        specs.append(_entry_spec(r))
    received_qty = sum(_num(r.get("entry_receiveqty") or r.get("receiveqty")) for r in rows)
    in_stock_qty = sum(_num(r.get("entry_invqty") or r.get("invqty")) for r in rows)
    return_qty = sum(_num(r.get("entry_returnqty") or r.get("returnqty")) for r in rows)
    total_amount = head.get("totalamount")
    total_all_amount = head.get("totalallamount")
    tax_amount = head.get("totaltaxamount")
    if total_amount is None:
        total_amount = sum(_num(r.get("entry_amount") or r.get("amount")) for r in rows)
    if total_all_amount is None:
        total_all_amount = sum(_num(r.get("entry_amountandtax") or r.get("amountandtax")) for r in rows)
    if tax_amount is None:
        tax_amount = sum(_num(r.get("entry_taxamount") or r.get("taxamount")) for r in rows)
    expected_qty = in_stock_qty if in_stock_qty > 0 else 0
    spec = _join_unique(specs)
    capacity, frequency = _parse_memory_spec(spec)
    return {
        "oa_order_no": billno, "batch_no": billno,
        "customer": "", "supplier": supplier,
        "brand": "", "model": "、".join(materials), "capacity": capacity, "frequency": frequency,
        "cond": "", "remark": f"金蝶采购订单 {billno}",
        "qty_expected": int(round(expected_qty)),
        "delivery_date": biztime,
        "oa_synced": 1, "oa_raw": {"billno": billno, "rows": rows},
        "kd_bill_status": _first(head, "billstatus"),
        "kd_close_status": _first(head, "closestatus"),
        "kd_pay_mode": _first(head, "paymode"),
        "kd_operator": _first(head, "operatorname", "operatornumber"),
        "kd_supplier_number": supplier_number,
        "kd_supplier_status": "",
        "kd_material_number": _join_unique(material_numbers),
        "kd_src_bill_no": _join_unique(src_bill_nos),
        "kd_specification": spec,
        "kd_total_amount": _money(total_amount),
        "kd_total_all_amount": _money(total_all_amount),
        "kd_tax_amount": _money(tax_amount),
        "kd_received_qty": round(received_qty, 2),
        "kd_in_stock_qty": round(in_stock_qty, 2),
        "kd_return_qty": round(return_qty, 2),
    }


def _group_orders(rows: list) -> list:
    """金蝶按物料分录行返回，同一单号多行 → 按 billno 归并成一张订单（保持出现顺序）。"""
    groups, order = {}, []
    for r in _flatten_orders(rows):
        bn = _first(r, "billno", "billNo")
        if not bn:
            continue
        if bn not in groups:
            groups[bn] = []
            order.append(bn)
        groups[bn].append(r)
    return [_map_order(bn, groups[bn]) for bn in order]


def _is_memory_order(order: dict) -> bool:
    """Only memory-module purchase orders should enter QC selection."""
    text = " ".join(str(order.get(k) or "") for k in (
        "model", "brand", "capacity", "frequency", "kd_specification",
        "kd_material_number", "remark"
    )).lower()
    if any(x in text for x in (
        "中央处理器", "处理器", "cpu", "网卡", "network card", "nic",
        "显卡", "图形卡", "graphics card", "gpu", "geforce", "radeon", "rtx", "gtx",
    )):
        return False
    if any(x in text for x in ("内存", "ddr", "pc4", "pc5", "rdimm", "udimm", "sodimm")):
        return True
    return False


def _filter_memory_orders(orders: list[dict]) -> list[dict]:
    return [o for o in orders if _is_memory_order(o)]


def _enrich_specs_from_sales_orders(orders: list[dict]) -> list[dict]:
    sales_billnos = []
    order_refs: list[tuple[dict, str, str]] = []
    material_refs: list[tuple[dict, str]] = []
    for order in orders:
        for row in ((order.get("oa_raw") or {}).get("rows") or []):
            if not isinstance(row, dict):
                continue
            sales_no = _first(row, "entry_salbillnumber", "salbillnumber",
                              "entry_srcbillnumber", "srcbillnumber")
            material_no = _first(row, "entry_materialnumber", "materialnumber",
                                 "entry_material_masterid_number", "material_masterid_number")
            if sales_no and material_no:
                sales_billnos.append(sales_no)
                order_refs.append((order, sales_no, material_no))
            if material_no:
                material_refs.append((order, material_no))
    if sales_billnos:
        try:
            specs = sales_spec_map(sales_billnos)
        except Exception as e:  # noqa: BLE001
            specs = {}
            log.warning("金蝶销售订单规格型号查询失败，尝试按物料编码兜底: %s", e)
        for order, sales_no, material_no in order_refs:
            spec = specs.get((sales_no, material_no))
            if not spec:
                continue
            existing = order.get("kd_specification") or ""
            order["kd_specification"] = _join_unique([existing, spec])
            cap, freq = _parse_memory_spec(order["kd_specification"])
            order["capacity"] = order.get("capacity") or cap
            order["frequency"] = order.get("frequency") or freq
    missing_materials = sorted({material_no for order, material_no in material_refs
                                if material_no and not order.get("kd_specification")})
    if missing_materials:
        try:
            by_material = sales_material_spec_map()
        except Exception as e:  # noqa: BLE001
            by_material = {}
            log.warning("金蝶销售订单物料规格兜底查询失败: %s", e)
        for order, material_no in material_refs:
            spec = by_material.get(material_no)
            if not spec:
                continue
            existing = order.get("kd_specification") or ""
            order["kd_specification"] = _join_unique([existing, spec])
            cap, freq = _parse_memory_spec(order["kd_specification"])
            order["capacity"] = cap
            order["frequency"] = freq
    return orders


def _resolve_supplier_names(orders: list[dict]) -> list[dict]:
    """用供应商查询接口把编码转成中文名称，失败则保留原始编码。"""
    codes = sorted({
        (o.get("kd_supplier_number") or o.get("supplier") or "").strip()
        for o in orders
        if (o.get("kd_supplier_number") or o.get("supplier") or "").strip()
    })
    if not codes:
        return orders
    try:
        suppliers = supplier_name_map(codes)
    except Exception as e:  # noqa: BLE001
        log.warning("金蝶供应商名称查询失败，保留供应商编码: %s", e)
        return orders
    for order in orders:
        code = (order.get("kd_supplier_number") or order.get("supplier") or "").strip()
        info = suppliers.get(code)
        if not info:
            continue
        order["kd_supplier_number"] = code
        order["supplier"] = info.get("name") or code
        order["kd_supplier_status"] = info.get("status") or ""
    return orders


def _recent_page_range(total: int, page_size: int, pages: int) -> range:
    last_page = max(1, math.ceil(max(0, int(total)) / max(1, int(page_size))))
    first_page = max(1, last_page - max(1, int(pages)) + 1)
    return range(first_page, last_page + 1)


def fetch_orders(only_audited: bool = True, page_size: int = 100,
                 max_pages: int | None = None, recent: bool = False) -> dict:
    """拉采购订单列表 → 本地订单字段。

    字段表要求 data.billno 必填；真实接口支持 billno="*" 返回列表。
    only_audited 当前不额外过滤，避免把金蝶状态枚举误判掉。
    """
    if not is_configured():
        return {"ok": False, "error": "金蝶未配置（.env 缺 KD_* 凭证）"}
    try:
        all_rows, total = [], 0
        first = query_orders(page_no=1, page_size=page_size, billno="*")
        total = int(first.get("totalCount") or 0)
        if recent and max_pages is not None:
            page_numbers = list(_recent_page_range(total, page_size, max_pages))
            for page_no in page_numbers:
                res = first if page_no == 1 else query_orders(
                    page_no=page_no, page_size=page_size, billno="*")
                all_rows.extend(res["rows"])
        else:
            page_no, res = 1, first
            while True:
                rows = res["rows"]
                all_rows.extend(rows)
                total = int(res.get("totalCount") or total or 0)
                if not rows or res.get("lastPage") is True or (total and len(all_rows) >= total):
                    break
                if max_pages is not None and page_no >= max(1, int(max_pages)):
                    break
                page_no += 1
                if page_no > 100:
                    raise RuntimeError("金蝶采购订单分页超过 100 页，已停止以避免异常循环")
                res = query_orders(page_no=page_no, page_size=page_size, billno="*")
        orders = _group_orders(all_rows)
        orders = _enrich_specs_from_sales_orders(orders)
        orders = _resolve_supplier_names(_filter_memory_orders(orders))
        log.info("金蝶采购订单%s：分录行 %d → 归并订单 %d 张（totalCount=%s）",
                 "（最新页）" if recent else "", len(all_rows), len(orders), total)
        return {"ok": True, "orders": orders, "total": total or len(orders)}
    except Exception as e:  # noqa: BLE001
        log.exception("金蝶拉单失败")
        return {"ok": False, "error": str(e)}


def fetch_order(billno: str) -> dict:
    """按单号拉单张。返回 {ok, order} 或 {ok:False, error}。"""
    if not is_configured():
        return {"ok": False, "error": "金蝶未配置（.env 缺 KD_* 凭证）"}
    bn = (billno or "").strip().replace("'", "")
    if not bn:
        return {"ok": False, "error": "单号为空"}
    try:
        res = query_orders(f"billno = '{bn}'", page_no=1, page_size=200, billno=bn)
        orders = _group_orders(res["rows"])
        orders = _enrich_specs_from_sales_orders(orders)
        orders = _resolve_supplier_names(_filter_memory_orders(orders))
        if not orders:
            return {"ok": False, "error": f"未找到采购单 {bn}"}
        return {"ok": True, "order": orders[0]}
    except Exception as e:  # noqa: BLE001
        log.exception("金蝶拉单(单张)失败")
        return {"ok": False, "error": str(e)}
