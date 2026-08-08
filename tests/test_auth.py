"""登录鉴权纯逻辑测试（不连数据库）：密码哈希 + current_user/require_* 门禁。"""
import pytest
from fastapi import HTTPException

from app import auth
from app.storage import db


def test_password_hash_deterministic_and_salted():
    salt = db._make_salt()
    h1 = db._hash_pw("secret", salt)
    assert h1 == db._hash_pw("secret", salt)          # 同密码同盐 → 同哈希（可校验）
    assert h1 != "secret"                             # 不是明文
    assert db._hash_pw("secret", db._make_salt()) != h1   # 换盐 → 不同（防彩虹表）
    assert db._hash_pw("wrong", salt) != h1               # 错密码 → 不同


class _FakeReq:
    def __init__(self, cookies):
        self.cookies = cookies


def test_current_user(monkeypatch):
    monkeypatch.setattr(db, "get_session",
                        lambda t: {"username": "u", "role": "admin"} if t == "good" else None)
    assert auth.current_user(_FakeReq({"sid": "good"}))["role"] == "admin"
    assert auth.current_user(_FakeReq({"sid": "bad"})) is None   # 无效 token
    assert auth.current_user(_FakeReq({})) is None               # 无 cookie


def test_require_login_401(monkeypatch):
    monkeypatch.setattr(db, "get_session", lambda t: None)
    with pytest.raises(HTTPException) as e:
        auth.require_login(_FakeReq({}))
    assert e.value.status_code == 401


def test_require_admin_403_for_operator():
    assert auth.require_admin({"username": "a", "role": "admin"})["role"] == "admin"
    with pytest.raises(HTTPException) as e:
        auth.require_admin({"username": "b", "role": "operator"})
    assert e.value.status_code == 403
