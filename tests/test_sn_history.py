"""同 SN 历史比对单测：同一 SN 历次日期变了 → 疑似被换芯片（防偷换第二道）。"""
from app.services import sn_history_diff


def _rec(drams, controller="", pcb=""):
    return {"storage_chips": [{"yyyyww": d} for d in drams],
            "controller_date": controller, "pcb_date": pcb}


def test_consistent_history_not_changed():
    recs = [_rec(["202540", "202540"], "202536", "202536"),
            _rec(["202540", "202540"], "202536", "202536")]
    d = sn_history_diff(recs)
    assert d["changed"] is False
    assert d["count"] == 2


def test_chip_dates_changed_flagged():
    # 第二次颗粒日期集合变了 → 疑似中途被换芯片
    recs = [_rec(["202540", "202540"]), _rec(["202540", "202510"])]
    assert sn_history_diff(recs)["changed"] is True


def test_controller_or_pcb_changed_flagged():
    recs = [_rec(["202540"], controller="202536"), _rec(["202540"], controller="202401")]
    assert sn_history_diff(recs)["changed"] is True


def test_order_insensitive_same_set():
    # 同一组日期只是顺序不同 → 不算变（按集合比）
    recs = [_rec(["202540", "202536"]), _rec(["202536", "202540"])]
    assert sn_history_diff(recs)["changed"] is False


def test_single_record_not_changed():
    assert sn_history_diff([_rec(["202540"])])["changed"] is False
