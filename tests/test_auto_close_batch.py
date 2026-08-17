from app.storage import db


class FakeCursor:
    def __init__(self, rowcount=1):
        self.calls = []
        self.rowcount = rowcount

    def execute(self, sql, params=()):
        self.calls.append((sql, params))


def test_auto_close_batch_uses_completed_quantity_threshold():
    cur = FakeCursor(rowcount=1)

    closed = db._auto_close_batch_if_complete(cur, 12)

    assert closed is True
    assert cur.calls
    sql, params = cur.calls[0]
    assert params == (12,)
    assert "UPDATE batches b" in sql
    assert "active=0" in sql
    assert "COUNT(*)" in sql
    assert ">= b.qty_expected" in sql


def test_auto_close_batch_ignores_missing_batch_id():
    cur = FakeCursor()

    assert db._auto_close_batch_if_complete(cur, None) is False
    assert cur.calls == []
