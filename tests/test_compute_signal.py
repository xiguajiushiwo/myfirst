"""compute_signal 单测：综合判定的核心（逐颗比较、严禁多数表决）。

最关键的一条：**单颗被换（日期不同）必须判不合格并定位到那颗**，绝不能被多数吸收。
"""
from app.recognition.date_parser import DateCode, _week_start_date
from app.services import compute_signal


def _dram(year, week, side="front", idx=1, status="ok"):
    """造一颗存储颗粒 DateCode（带 _side/idx，compute_signal 定位要用）。"""
    c = DateCode(raw=str(week), code_type="dram", year=year, week=week,
                 week_start=_week_start_date(year, week) if week else "",
                 confidence=0.9, source_text="", digit_format="YWW", status=status)
    c._side = side
    c.idx = idx
    return c


def test_all_same_pass():
    codes = [_dram(2025, 40, idx=i) for i in range(1, 6)]
    sig = compute_signal(codes, 10)
    assert sig["status"] == "pass"
    assert sig["spread_weeks"] == 0.0
    assert sig["blind"] == 0


def test_within_threshold_pass():
    # 正面 25 周、背面 28 周，差 3 周 ≤ 10 → 合格（正常同批）
    codes = [_dram(2025, 25, "front", 1), _dram(2025, 28, "back", 1)]
    sig = compute_signal(codes, 10)
    assert sig["status"] == "pass"


def test_single_swapped_chip_fails_and_locates():
    """核心：满板 40 周里混进 1 颗 10 周（被换）→ 判不合格且定位到第 16 颗。"""
    codes = [_dram(2025, 40, "front", i) for i in range(1, 16)]
    codes.append(_dram(2025, 10, "front", 16))       # 被换的那颗
    sig = compute_signal(codes, 10)
    assert sig["status"] == "fail"                    # 绝不能被多数吸收
    assert sig["spread_weeks"] > 10
    assert "第16颗" in sig["message"]                  # 定位到具体哪颗


def test_blind_chip_flagged_but_not_fail():
    # 两颗正常 + 一颗读不出（盲点）→ 判定看能读出的（pass），盲点单列提示人工
    codes = [_dram(2025, 40, "front", 1), _dram(2025, 40, "front", 2),
             _dram(0, 0, "back", 3, status="raw")]
    sig = compute_signal(codes, 10)
    assert sig["status"] == "pass"
    assert sig["blind"] == 1
    assert sig["blind_desc"] == ["背面第3颗颗粒"]
    assert "请人工确认" in sig["message"]


def test_too_few_unknown():
    sig = compute_signal([_dram(2025, 40)], 10)
    assert sig["status"] == "unknown"


def test_threshold_boundary():
    # 恰好等于阈值 → 合格（≤ 含边界）
    codes = [_dram(2025, 10, "front", 1), _dram(2025, 20, "front", 2)]  # 差 10 周
    sig = compute_signal(codes, 10)
    assert sig["status"] == "pass"
    # 差 11 周 → 不合格
    codes2 = [_dram(2025, 10, "front", 1), _dram(2025, 21, "front", 2)]
    assert compute_signal(codes2, 10)["status"] == "fail"
