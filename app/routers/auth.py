"""认证接口：注册 / 登录 / 退出 / 当前用户 / 用户审核（管理员）。

- 注册即 pending，须管理员审核通过才能登录。
- 登录成功发会话 token，写 HttpOnly cookie（浏览器同源自动带，前端 fetch 无需改）。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse

from ..auth import COOKIE_NAME, current_user, require_admin
from ..storage import db

router = APIRouter()

_TTL_H = int(os.environ.get("SESSION_TTL_HOURS", "12") or 12)


@router.post("/api/register")
def register(username: str = Form(...), password: str = Form(...), phone: str = Form("")):
    """自助注册 → 待审核。用户名重复报错。手机号可选（审核时联系用）。"""
    ok, msg = db.create_user(username, password, role="operator", status="pending", phone=phone)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=200)
    db.add_audit(username, "register", username, "自助注册待审核")
    return {"ok": True, "message": "注册成功，请等待管理员审核通过后再登录"}


@router.post("/api/login")
def login(response: Response, username: str = Form(...), password: str = Form(...)):
    """登录：密码对 + 状态 approved 才放行，发会话 cookie。"""
    u = db.verify_login(username, password)
    if not u:
        return JSONResponse({"ok": False, "error": "账号或密码错误"}, status_code=200)
    if u["status"] == "pending":
        return JSONResponse({"ok": False, "error": "账号待管理员审核，通过后才能登录"}, status_code=200)
    if u["status"] != "approved":
        return JSONResponse({"ok": False, "error": "账号已被停用，请联系管理员"}, status_code=200)
    token = db.create_session(u["username"], u["role"], _TTL_H)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                        max_age=_TTL_H * 3600, path="/")
    db.add_audit(u["username"], "login", u["username"])
    return {"ok": True, "username": u["username"], "role": u["role"]}


@router.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        db.delete_session(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/api/me")
def me(request: Request):
    """当前登录用户（前端显示用户名/角色、按角色显隐菜单）。未登录返回 authenticated=False。"""
    u = current_user(request)
    if not u:
        return {"authenticated": False}
    return {"authenticated": True, "username": u["username"], "role": u["role"]}


# --------------------- 以下需管理员 ---------------------

@router.get("/api/users")
def users(status: str = "", _admin: dict = Depends(require_admin)):
    return {"users": db.list_users(status or None)}


@router.post("/api/users/{uid}/review")
def review_user(uid: int, action: str = Form(...), admin: dict = Depends(require_admin)):
    """审核：action=approve/reject。"""
    status = {"approve": "approved", "reject": "rejected"}.get(action)
    if not status:
        return JSONResponse({"ok": False, "error": "action 只能是 approve/reject"}, status_code=200)
    ok = db.set_user_status(uid, status, by=admin["username"])
    db.add_audit(admin["username"], "review_user", str(uid), status)
    return {"ok": ok}


@router.post("/api/users/{uid}/reset_pw")
def reset_pw(uid: int, password: str = Form(...), admin: dict = Depends(require_admin)):
    ok = db.set_user_password(uid, password)
    db.add_audit(admin["username"], "reset_pw", str(uid))
    return {"ok": ok}
