import pytest

from app import services
from app.routers import recognition


def _batch(brand, capacity="64GB", frequency="5600"):
    return {
        "id": 1,
        "brand": brand,
        "model": f"{brand} 内存条",
        "capacity": capacity,
        "frequency": frequency,
    }


def test_samsung_64g_5600_uses_calibrated_template():
    assert services._resolve_product_template(_batch("Samsung"), None) == "samsung-4up-0808"


def test_samsung_spec_field_can_drive_template_selection():
    batch = _batch("三星", capacity="", frequency="")
    batch["kd_specification"] = "64G 5600"

    assert services._resolve_product_template(batch, None) == "samsung-4up-0808"


def test_samsung_other_frequency_is_blocked():
    batch = _batch("三星", capacity="", frequency="")
    batch["kd_specification"] = "64G 3200"

    with pytest.raises(ValueError, match="当前只配置了三星 64GB 5600"):
        services._resolve_product_template(batch, None)


def test_hynix_64g_5600_is_blocked_until_calibrated():
    with pytest.raises(ValueError, match="海力士识别模板已删除"):
        services._resolve_product_template(_batch("SK Hynix"), None)


def test_other_hynix_spec_cannot_reuse_pending_5600_profile():
    with pytest.raises(ValueError, match="当前只配置了海力士 64GB 5600"):
        services._resolve_product_template(_batch("海力士", capacity="32GB", frequency="4800"), None)


def test_mixed_brand_order_is_blocked():
    with pytest.raises(ValueError, match="混合物料"):
        services._resolve_product_template(_batch("三星 / 海力士"), None)


def test_order_template_endpoint_reports_no_hynix_template(monkeypatch):
    monkeypatch.setattr(recognition.db, "get_batch", lambda _batch_id: _batch("SK Hynix"))

    result = recognition.template_for_order(1)

    assert result["ok"] is True
    assert result["ready"] is False
    assert result["template"]["id"] == ""
    assert result["template"]["requirements"] == []


def test_order_template_endpoint_returns_samsung_profile(monkeypatch):
    monkeypatch.setattr(recognition.db, "get_batch", lambda _batch_id: _batch("Samsung"))

    result = recognition.template_for_order(1)

    assert result["ready"] is True
    assert result["template"]["id"] == "samsung-4up-0808"
