"""采购订单归集 + OA 桩 单测（纯逻辑，不连数据库）。

覆盖：
- build_record 把订单登记的 model/supplier/customer/品牌等**带进每条记录**（订单优先，回退标签）。
- OA 未配置时 fetch_* 明确返回 ok:False（绝不假成功、不造数据）。
"""
from app import kingdee, oa
from app.routers import batches
from app.services import build_record


def _args(label, batch=None):
    """build_record 的最小入参：识别为空、外观OK、给定 label/batch。"""
    rec = {"dates": {"date_ok": True}}
    insp = {"ok": True, "comp_ok": True, "gold_finger_ok": True, "chip_mark_ok": True}
    return build_record(rec, insp, label, "质检员A", batch=batch)


def test_order_fields_carried_into_record():
    # 订单登记了 品牌/型号/供应商/客户 → 每条记录都继承（订单优先）
    batch = {"brand": "Samsung", "model": "M321R", "supplier": "供应商甲",
             "customer": "客户乙", "capacity": "64GB", "frequency": "DDR5-4800",
             "batch_no": "PO-2026-001", "cond": "全新", "remark": "香港货"}
    r = _args({"sn": "SN123"}, batch=batch)
    assert r["brand"] == "Samsung"
    assert r["model"] == "M321R"
    assert r["supplier"] == "供应商甲"
    assert r["customer"] == "客户乙"
    assert r["batch_no"] == "PO-2026-001"


def test_model_falls_back_to_label_when_order_blank():
    # 订单没填型号 → 回退用标签读出的型号；供应商标签读不出 → 空
    batch = {"brand": "Hynix"}
    r = _args({"sn": "SN9", "model": "HMCG"}, batch=batch)
    assert r["model"] == "HMCG"
    assert r["supplier"] == ""


def test_no_batch_uses_label_only():
    r = _args({"sn": "SN0", "brand": "Micron", "model": "MTC"})
    assert r["brand"] == "Micron"
    assert r["model"] == "MTC"
    assert r["supplier"] == ""


def test_oa_not_configured_returns_error(monkeypatch):
    # 未配置 → 明确 ok:False，绝不假成功
    monkeypatch.setattr(oa, "_BASE_URL", "")
    monkeypatch.setattr(oa, "_TOKEN", "")
    assert oa.is_configured() is False
    r = oa.fetch_orders()
    assert r["ok"] is False and "未配置" in r["error"]
    assert oa.fetch_order("PO-1")["ok"] is False


def test_oa_map_shape():
    # 映射产出的 dict 含本地订单所有键（文档到了只改取值，不改形状）
    m = oa._map_oa_to_order({"orderNo": "PO-9", "customer": "C", "qty": 50})
    for k in ("batch_no", "oa_order_no", "customer", "supplier", "brand", "model",
              "capacity", "frequency", "cond", "remark", "qty_expected",
              "delivery_date", "oa_synced", "oa_raw"):
        assert k in m
    assert m["oa_order_no"] == "PO-9" and m["qty_expected"] == 50


def test_kingdee_maps_nested_billentry_shape():
    rows = [{
        "billno": "CGDD-1",
        "supplier": {"name": "供应商A"},
        "biztime": "2026-07-29 10:20:30",
        "billentry": [
            {"qty": 20, "invqty": 2, "material": {"name": "DDR5 32GB"}},
            {"qty": "30.0", "invqty": "3.0", "materialname": "DDR5 64GB"},
        ],
    }]
    orders = kingdee._group_orders(rows)
    assert len(orders) == 1
    assert orders[0]["batch_no"] == "CGDD-1"
    assert orders[0]["supplier"] == "供应商A"
    assert orders[0]["delivery_date"] == "2026-07-29"
    assert orders[0]["qty_expected"] == 5
    assert orders[0]["kd_in_stock_qty"] == 5
    assert orders[0]["model"] == "DDR5 32GB、DDR5 64GB"


def test_kingdee_preserves_supplier_number():
    rows = [{
        "billno": "CGDD-2",
        "providersuppliernumber": "SUS00000323",
        "billentry": [{"qty": 10, "invqty": 1, "materialname": "DDR5"}],
    }]
    order = kingdee._group_orders(rows)[0]
    assert order["supplier"] == "SUS00000323"
    assert order["kd_supplier_number"] == "SUS00000323"
    assert order["kd_supplier_status"] == ""


def test_kingdee_resolves_supplier_name(monkeypatch):
    monkeypatch.setattr(kingdee, "supplier_name_map", lambda numbers: {
        "SUS00000323": {"name": "天津天星科技发展有限公司", "status": "合格"}
    })
    orders = kingdee._resolve_supplier_names([{
        "supplier": "SUS00000323",
        "kd_supplier_number": "SUS00000323",
        "kd_supplier_status": "",
    }])
    assert orders[0]["supplier"] == "天津天星科技发展有限公司"
    assert orders[0]["kd_supplier_number"] == "SUS00000323"
    assert orders[0]["kd_supplier_status"] == "合格"


def test_kingdee_parses_memory_spec():
    assert kingdee._parse_memory_spec("64G 5600") == ("64GB", "5600")
    assert kingdee._parse_memory_spec("128GB DDR5-6400") == ("128GB", "6400")
    assert kingdee._parse_memory_spec("RTX 5090") == ("", "")


def test_kingdee_enriches_purchase_specs_from_sales(monkeypatch):
    monkeypatch.setattr(kingdee, "sales_spec_map", lambda billnos: {
        ("XS-1", "1026"): "64G 5600",
    })
    rows = [{
        "billno": "CGDD-3",
        "billentry": [{
            "qty": 10,
            "invqty": 10,
            "materialname": "DDR5",
            "materialnumber": "1026",
            "salbillnumber": "XS-1",
        }],
    }]

    orders = kingdee._enrich_specs_from_sales_orders(kingdee._group_orders(rows))

    assert orders[0]["kd_specification"] == "64G 5600"
    assert orders[0]["capacity"] == "64GB"
    assert orders[0]["frequency"] == "5600"


def test_kingdee_enriches_purchase_specs_by_material_when_source_missing(monkeypatch):
    monkeypatch.setattr(kingdee, "sales_material_spec_map", lambda: {"1012": "128G 6400"})
    rows = [{
        "billno": "CGDD-4",
        "billentry": [{
            "qty": 10,
            "invqty": 10,
            "materialname": "三星（SAM）内存条",
            "materialnumber": "1012",
        }],
    }]

    orders = kingdee._enrich_specs_from_sales_orders(kingdee._group_orders(rows))

    assert orders[0]["kd_specification"] == "128G 6400"
    assert orders[0]["capacity"] == "128GB"
    assert orders[0]["frequency"] == "6400"


def test_kingdee_filters_non_memory_orders():
    rows = [
        {"billno": "CGDD-MEM", "billentry": [{"qty": 10, "invqty": 10, "materialname": "三星（SAM）内存条"}]},
        {"billno": "CGDD-CPU", "billentry": [{"qty": 2, "invqty": 2, "materialname": "中央处理器"}]},
        {"billno": "CGDD-NIC", "billentry": [{"qty": 4, "invqty": 4, "materialname": "网卡"}]},
        {"billno": "CGDD-GPU", "billentry": [{"qty": 4, "invqty": 4, "materialname": "RTX 5090 显卡"}]},
    ]
    orders = kingdee._filter_memory_orders(kingdee._group_orders(rows))
    assert [o["batch_no"] for o in orders] == ["CGDD-MEM"]


def test_synced_gpu_is_not_restored_by_frequency_number():
    assert batches._is_memory_order({
        "model": "NVIDIA RTX 5090 显卡",
        "frequency": "5090",
        "oa_synced": 1,
    }) is False


def test_kingdee_query_uses_billno_body(monkeypatch):
    calls = []

    class Resp:
        def json(self):
            return {"status": True, "data": {"rows": [], "totalCount": 0}}

    def fake_post(url, json, headers, timeout):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return Resp()

    monkeypatch.setattr(kingdee, "_BASE", "https://example.test")
    monkeypatch.setattr(kingdee, "_ORDER_API", "/kapi/v2/pm/pm_purorderbill/query")
    monkeypatch.setattr(kingdee, "_IDENTITY", "ident")
    monkeypatch.setattr(kingdee, "_ACCT", "acct")
    monkeypatch.setattr(kingdee, "_token", {"value": "tok", "exp": 9999999999.0})
    monkeypatch.setattr(kingdee.requests, "post", fake_post)

    kingdee.query_orders(page_no=1, page_size=10, billno="CGDD-1")

    body = calls[0]["json"]
    assert body["data"] == {"billno": "CGDD-1"}
    assert body["pageSize"] == 10
    assert "filter" not in body["data"]
    assert calls[0]["headers"]["accessToken"] == "tok"
    assert calls[0]["headers"]["x-acgw-identity"] == "ident"


def test_kingdee_query_suppliers_uses_number_array(monkeypatch):
    calls = []

    class Resp:
        def json(self):
            return {"status": True, "data": {"rows": [], "totalCount": 0}}

    def fake_post(url, json, headers, timeout):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return Resp()

    monkeypatch.setattr(kingdee, "_BASE", "https://example.test")
    monkeypatch.setattr(kingdee, "_SUPPLIER_API", "/kapi/v2/yk70/basedata/bd_supplier/query")
    monkeypatch.setattr(kingdee, "_IDENTITY", "ident")
    monkeypatch.setattr(kingdee, "_ACCT", "acct")
    monkeypatch.setattr(kingdee, "_token", {"value": "tok", "exp": 9999999999.0})
    monkeypatch.setattr(kingdee.requests, "post", fake_post)

    kingdee.query_suppliers(["SUS00000323", "SUS00000752"], page_no=1, page_size=10)

    body = calls[0]["json"]
    assert calls[0]["url"].endswith("/kapi/v2/yk70/basedata/bd_supplier/query")
    assert body["data"] == {"number": ["SUS00000323", "SUS00000752"]}
    assert body["pageSize"] == 10


def test_kingdee_fetch_orders_uses_star_billno(monkeypatch):
    calls = []

    def fake_query_orders(*, page_no, page_size, billno, filter_str="1 = 1"):
        calls.append({"page_no": page_no, "page_size": page_size, "billno": billno})
        if page_no == 1:
            return {
                "rows": [{"billno": "SZZDT-CG-1", "billentry": [{"qty": 1, "materialname": "DDR5 内存条"}]}],
                "totalCount": 2,
                "lastPage": False,
            }
        return {
            "rows": [{"billno": "SZZDT-CG-2", "billentry": [{"qty": 2, "materialname": "海力士（SK）内存条"}]}],
            "totalCount": 2,
            "lastPage": True,
        }

    monkeypatch.setattr(kingdee, "_BASE", "https://example.test")
    monkeypatch.setattr(kingdee, "_CID", "cid")
    monkeypatch.setattr(kingdee, "_SECRET", "secret")
    monkeypatch.setattr(kingdee, "_USER", "user")
    monkeypatch.setattr(kingdee, "_IDENTITY", "ident")
    monkeypatch.setattr(kingdee, "query_orders", fake_query_orders)

    res = kingdee.fetch_orders(page_size=20)

    assert res["ok"] is True
    assert calls == [
        {"page_no": 1, "page_size": 20, "billno": "*"},
        {"page_no": 2, "page_size": 20, "billno": "*"},
    ]
    assert res["total"] == 2
    assert len(res["orders"]) == 2
    assert res["orders"][0]["batch_no"] == "SZZDT-CG-1"
