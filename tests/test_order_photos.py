from app.routers import records


def test_order_records_endpoint_groups_photos_by_batch(monkeypatch):
    monkeypatch.setattr(records.db, "get_batch", lambda batch_id: {
        "id": batch_id,
        "batch_no": "PO-2026-001",
    })
    monkeypatch.setattr(records.db, "list_records_by_batch", lambda batch_id, limit: [{
        "id": 11,
        "batch_id": batch_id,
        "front_img": "/archive/20260812/order_7/run/front.jpg",
        "annotated_front": "/archive/20260812/order_7/run/front_annotated.png",
    }])

    result = records.records_by_order(7)

    assert result["ok"] is True
    assert result["order"]["batch_no"] == "PO-2026-001"
    assert result["records"][0]["batch_id"] == 7
    assert result["count"] == 1
