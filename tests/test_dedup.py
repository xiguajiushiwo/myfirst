"""防重复放盘单测：本批同一盘（4 根 SN 一致）不重复入库。"""
from app.services import tray_fingerprint, _is_dup_tray, _SEEN_TRAYS


def test_fingerprint_order_insensitive():
    # 4 根 SN 顺序不同（换了槽位）→ 指纹相同（还是同一盘）
    assert tray_fingerprint(["A", "B", "C", "D"]) == tray_fingerprint(["D", "C", "B", "A"])


def test_fingerprint_too_few_sns_none():
    # 有效 SN < 2（读不到）→ None，不去重，避免空 SN 误判
    assert tray_fingerprint([]) is None
    assert tray_fingerprint(["", "", ""]) is None
    assert tray_fingerprint(["A"]) is None


def test_dup_detected_within_batch():
    _SEEN_TRAYS.clear()
    fp = tray_fingerprint(["SN1", "SN2", "SN3"])
    assert _is_dup_tray(10, fp) is False      # 第一次：新盘
    assert _is_dup_tray(10, fp) is True       # 同批再放同一盘：重复


def test_different_tray_not_dup():
    _SEEN_TRAYS.clear()
    fp1 = tray_fingerprint(["SN1", "SN2"])
    fp2 = tray_fingerprint(["SN3", "SN4"])
    assert _is_dup_tray(7, fp1) is False
    assert _is_dup_tray(7, fp2) is False      # 换了别的条 → 不重复


def test_new_batch_resets():
    _SEEN_TRAYS.clear()
    fp = tray_fingerprint(["SN1", "SN2"])
    assert _is_dup_tray(1, fp) is False
    # 隔天新批次（新 batch_id）→ 合法复检不算重复
    assert _is_dup_tray(2, fp) is False


def test_no_batch_or_no_fp_never_dup():
    _SEEN_TRAYS.clear()
    assert _is_dup_tray(None, "x") is False   # 无批次不去重
    assert _is_dup_tray(5, None) is False      # 无指纹不去重
