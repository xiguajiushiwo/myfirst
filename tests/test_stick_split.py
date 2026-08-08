"""一盘拆 N 条记录 · 逐根判定单测（_stick_breakdown）。

最关键两条：
  ①**跨根不比较**：两根不同批次(日期差很大)各自都应合格，绝不能因为放在同一盘里互相超差被误判。
  ②**根内防偷换**：某一根里混进 1 颗被换的芯片 → 只有那根 fail 并定位，另一根照常 pass。
"""
from app.recognition.date_parser import DateCode, _week_start_date
from app.services import _stick_breakdown, compute_signal


def _dram(year, week, slot, side="front", idx=1, status="ok"):
    c = DateCode(raw=str(week), code_type="dram", year=year, week=week,
                 week_start=_week_start_date(year, week) if week else "",
                 confidence=0.9, source_text="", digit_format="YWW", status=status)
    c._side = side
    c.idx = idx
    c.slot = slot
    return c


def test_cross_stick_not_compared():
    """槽0 全 40 周、槽1 全 10 周（差 30 周，不同批次）：逐根各自 pass。

    对照：把两根混在一起 compute_signal 会 fail（spread 30）——这正是要避免的误判。
    """
    codes = ([_dram(2025, 40, slot=0, idx=i) for i in range(1, 6)] +
             [_dram(2025, 10, slot=1, idx=i) for i in range(1, 6)])
    # 混一起（旧的整图逻辑）会误判不合格：
    assert compute_signal(codes, 10)["status"] == "fail"
    # 逐根拆开后：两根各自合格
    sticks = _stick_breakdown(codes, 10)
    assert len(sticks) == 2
    assert [s["pos"] for s in sticks] == [1, 2]
    assert all(s["signal"]["status"] == "pass" for s in sticks)


def test_swap_in_one_stick_only_fails_that_stick():
    """槽0 混进 1 颗被换芯片 → 仅槽0 fail 并定位；槽1 全 pass。"""
    codes = [_dram(2025, 40, slot=0, idx=i) for i in range(1, 10)]
    codes.append(_dram(2025, 5, slot=0, idx=10))            # 槽0 被换的那颗
    codes += [_dram(2025, 34, slot=1, idx=i) for i in range(1, 10)]
    by_pos = {s["pos"]: s for s in _stick_breakdown(codes, 10)}
    assert by_pos[1]["signal"]["status"] == "fail"          # 槽0 不合格
    assert "第10颗" in by_pos[1]["signal"]["message"]        # 定位到那颗
    assert by_pos[2]["signal"]["status"] == "pass"          # 槽1 不受影响


def test_counts_and_dates_per_stick():
    codes = ([_dram(2025, 40, slot=0, idx=i) for i in range(1, 4)] +
             [_dram(2025, 36, slot=1, idx=i) for i in range(1, 6)])
    sticks = _stick_breakdown(codes, 10)
    assert sticks[0]["counts"]["dram"] == 3
    assert sticks[1]["counts"]["dram"] == 5
    # 每根各自的结构化日期（storage_chips 只含本根颗粒）
    assert len(sticks[0]["dates"]["storage_chips"]) == 3
    assert len(sticks[1]["dates"]["storage_chips"]) == 5


def test_no_slot_returns_empty():
    """规则模式/单根(slot=-1) → 不产生逐根拆分（走整图一条记录）。"""
    codes = [_dram(2025, 40, slot=-1, idx=i) for i in range(1, 4)]
    assert _stick_breakdown(codes, 10) == []


def test_blind_chip_scoped_to_its_stick():
    """槽1 有一颗读不出(盲点)：只算进槽1 的 blind，不影响槽0。"""
    codes = ([_dram(2025, 40, slot=0, idx=i) for i in range(1, 4)] +
             [_dram(2025, 40, slot=1, idx=1), _dram(2025, 40, slot=1, idx=2),
              _dram(0, 0, slot=1, idx=3, status="raw")])
    by_pos = {s["pos"]: s for s in _stick_breakdown(codes, 10)}
    assert by_pos[1]["signal"]["blind"] == 0
    assert by_pos[2]["signal"]["blind"] == 1
    assert by_pos[2]["signal"]["blind_desc"] == ["正面第3颗颗粒"]
