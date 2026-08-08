"""登录鉴权：读 cookie 会话 + FastAPI 依赖（require_login / require_admin）。

密码哈希与会话存储在 storage.db（users/sessions 表）。本模块只管
「从请求里认出当前是谁、够不够权限」，供路由与中间件复用。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from .storage import db

COOKIE_NAME = "sid"


def current_user(request: Request) -> dict | None:
    """从 cookie 取出当前登录用户 {username, role}；未登录/会话失效返回 None。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        return db.get_session(token)
    except Exception:  # noqa: BLE001 —— DB 抖动时按未登录处理，不 500
        return None


def require_login(request: Request) -> dict:
    """需要已登录，否则 401。"""
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="未登录")
    return u


def require_admin(user: dict = Depends(require_login)) -> dict:
    """需要管理员角色，否则 403。"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
