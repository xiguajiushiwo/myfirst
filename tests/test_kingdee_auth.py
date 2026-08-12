import pytest

from app import kingdee


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_millisecond_token_lifetime_is_converted_to_seconds(monkeypatch):
    monkeypatch.setattr(kingdee.time, "time", lambda: 1000.0)
    monkeypatch.setattr(kingdee.requests, "post", lambda *_args, **_kwargs: _Response({
        "status": True,
        "data": {"access_token": "token", "expires_in": 7_199_977},
    }))
    monkeypatch.setattr(kingdee, "is_configured", lambda: True)
    kingdee._token.update(value="", exp=0.0)

    assert kingdee.get_token(force=True) == "token"
    assert kingdee._token["exp"] == pytest.approx(8199.977)


def test_query_refreshes_expired_token_and_retries(monkeypatch):
    token_calls = []
    responses = iter([
        _Response({"status": False, "message": "AccessToken认证不通过，token已过期"}),
        _Response({"status": True, "data": {"rows": [], "totalCount": 0}}),
    ])

    def fake_get_token(force=False):
        token_calls.append(force)
        return "new" if force else "cached"

    monkeypatch.setattr(kingdee, "get_token", fake_get_token)
    monkeypatch.setattr(kingdee.requests, "post", lambda *_args, **_kwargs: next(responses))

    result = kingdee.query_orders(page_no=1, page_size=100)

    assert result["rows"] == []
    assert token_calls == [False, True, False]


def test_recent_sync_uses_last_pages():
    assert list(kingdee._recent_page_range(total=454, page_size=100, pages=1)) == [5]
    assert list(kingdee._recent_page_range(total=454, page_size=100, pages=2)) == [4, 5]
    assert list(kingdee._recent_page_range(total=0, page_size=100, pages=1)) == [1]
