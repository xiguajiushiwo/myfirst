"""date_parser 纯逻辑单测：日期码解码/合法性/YYYYWW 归一。

这些是判定链最底层的解码逻辑，改一行可能悄悄改错判定，必须锁死。
不依赖 paddle / 网络，跑得快。
"""
from app.recognition.date_parser import (
    _decode_yww, _decode_yyww, _valid_week, to_yyyyww,
)

YEAR = 2026  # 当前年基准（解码用它裁掉未来年份）


class TestDecodeYWW:
    """3 位 YWW：首位=年份个位，后两位=周。"""

    def test_basic(self):
        assert _decode_yww("540", YEAR) == (2025, 40)   # 5→2025, 40 周
        assert _decode_yww("534", YEAR) == (2025, 34)
        assert _decode_yww("128", YEAR) == (2021, 28)   # demo5 的 SEC128

    def test_invalid_week(self):
        assert _decode_yww("199", YEAR) is None          # 第 99 周非法
        assert _decode_yww("100", YEAR) is None          # 第 0 周非法

    def test_year_not_in_future(self):
        # 个位为 9 的最近年份不应超过 当前年+1
        y, w = _decode_yww("912", YEAR)
        assert y <= YEAR + 1 and w == 12


class TestDecodeYYWW:
    """4 位 YYWW：前两位年、后两位周。"""

    def test_basic(self):
        assert _decode_yyww("2517", YEAR) == (2025, 17)
        assert _decode_yyww("2129", YEAR) == (2021, 29)

    def test_century_rule(self):
        assert _decode_yyww("9940", YEAR) == (1999, 40)  # 70-99 → 19xx

    def test_invalid_week(self):
        assert _decode_yyww("2599", YEAR) is None
        assert _decode_yyww("2500", YEAR) is None

    def test_future_year_rejected(self):
        assert _decode_yyww("3010", YEAR) is None         # 2030 > 2027


class TestValidWeek:
    def test_range(self):
        assert _valid_week(1) and _valid_week(53)
        assert not _valid_week(0)
        assert not _valid_week(54)


class TestToYYYYWW:
    def test_ok(self):
        assert to_yyyyww(2025, 40) == "202540"
        assert to_yyyyww(2021, 8) == "202108"            # 周补零

    def test_empty_on_invalid(self):
        assert to_yyyyww(2025, 0) == ""
        assert to_yyyyww(0, 40) == ""
        assert to_yyyyww(2025, 54) == ""
