"""海康相机【瘦客户端】：把所有相机调用转发到独立的相机子进程（cam_service，127.0.0.1:CAM_PORT）。

为什么这样拆：海康 MVS SDK 某次调用会卡死在 native 层、不按 timeout 返回，过去它跑在主服务进程里，
一卡就把预览/抓拍全堵死（"HTTP 200 但 0 帧"），只能重启整个系统。现在 SDK 被隔离进 cam_service
子进程；主服务**永不 import SDK、永不直接碰相机**，只通过本机 HTTP 向子进程要帧/下指令。子进程卡死
→ cam_supervisor 秒级 taskkill 重启它，主服务与网页无感。

本模块**保持与旧版完全一致的模块级函数签名**（status/snapshot/mjpeg/capture_both/set_exposure/
get_exposure/set_gain/get_gain/set_orient/get_orient/pause/resume/close/get_camera），使
routers/cameras.py、motion_trigger.py、server.py 零改动。传输用 stdlib urllib，不引入新依赖。
"""
from __future__ import annotations

import logging
import os
import time
import urllib.parse
import urllib.request

log = logging.getLogger("yxq.hik")

CAM_PORT = int(os.environ.get("CAM_PORT", "8811"))
_BASE = f"http://127.0.0.1:{CAM_PORT}"
_ROLES = ("front", "back")

# 方向校正模式（与子进程 hik_sdk 一致，供前端/参数校验复用）
_ORIENT_MODES = ("none", "fliph", "flipv", "rot180")


# --------------------- 底层 HTTP 转发 ---------------------

def _get(path: str, params: dict | None = None, timeout: float = 5.0) -> bytes:
    url = _BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _get_json(path: str, params: dict | None = None, timeout: float = 5.0) -> dict:
    import json
    return json.loads(_get(path, params, timeout).decode("utf-8"))


def _post_json(path: str, data: dict, timeout: float = 8.0) -> dict:
    import json
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(_BASE + path, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _norm_role(role: str) -> str:
    return role if role in _ROLES else "front"


# --------------------- 瘦客户端相机对象（供 motion_trigger 用 grab_array/_recover）---------------------

class _ClientCam:
    """代表子进程里的一台相机，本地只做"要帧/探活"。grab_array 从子进程 /frame 取最新帧并解码。"""

    def __init__(self, role: str):
        self.role = role

    def grab_array(self, timeout_ms: int = 3000):
        """取该角色最新一帧 → BGR np 数组。子进程后台已持续抓帧，这里只是拿缓冲最新帧。"""
        import cv2
        import numpy as np
        raw = _get("/frame", {"role": self.role}, timeout=max(1.0, timeout_ms / 1000.0))
        arr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            raise RuntimeError("解码子进程帧失败")
        return arr

    def _recover(self):
        """兼容旧接口：真正的重连由子进程后台线程/看门狗负责，这里空动作即可。"""
        return


_cams: dict[str, _ClientCam] = {}


def get_camera(role: str = "front") -> _ClientCam:
    role = _norm_role(role)
    cam = _cams.get(role)
    if cam is None:
        cam = _ClientCam(role)
        _cams[role] = cam
    return cam


# --------------------- 模块级 API（签名与旧版一致，全部转发子进程）---------------------

def status() -> dict:
    """相机可用性 + 设备表 + 子进程抓帧健康度。子进程不可达则返回 sdk=False（不抛错）。"""
    try:
        return _get_json("/status", timeout=4.0)
    except Exception as e:  # noqa: BLE001
        return {"sdk": False, "error": f"相机子进程不可达：{e}", "devices": [],
                "roles": {}, "orient": {}}


def snapshot(path: str, role: str = "front", **kw) -> str:
    """抓某角色一帧存到 path（子进程写盘，与主进程同机）。"""
    data = {"path": path, "role": _norm_role(role)}
    if "quality" in kw and kw["quality"] is not None:
        data["quality"] = int(kw["quality"])
    r = _post_json("/snapshot", data, timeout=float(kw.get("timeout_ms", 5000)) / 1000.0 + 3)
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "抓拍失败"))
    return path


def capture_both(front_path: str, back_path: str, **kw) -> dict:
    """同时抓 上(正面)/下(反面) 各一帧（子进程从两路近实时缓冲取，正反不错位）。"""
    data = {"front_path": front_path, "back_path": back_path}
    if "quality" in kw and kw["quality"] is not None:
        data["quality"] = int(kw["quality"])
    r = _post_json("/capture_both", data, timeout=10.0)
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "双相机抓拍失败"))
    return {"front": front_path, "back": back_path}


def mjpeg(role: str = "front", **kw):
    """实时预览：代理子进程 /preview 的 MJPEG 流，逐块转发给前端 <img>。

    返回一个字节块生成器，供 StreamingResponse 直接用（media_type=multipart/...）。
    子进程不可达/断流时结束生成器，前端 <img> onerror 会自动重连。
    """
    role = _norm_role(role)
    params = {"role": role}
    if kw.get("quality") is not None:
        params["quality"] = int(kw["quality"])
    if kw.get("max_fps") is not None:
        params["max_fps"] = int(kw["max_fps"])
    url = _BASE + "/preview?" + urllib.parse.urlencode(params)

    def gen():
        try:
            with urllib.request.urlopen(url, timeout=10.0) as resp:
                while True:
                    chunk = resp.read1(16384)
                    if not chunk:
                        break
                    yield chunk
        except Exception as e:  # noqa: BLE001
            log.warning("预览代理结束 role=%s：%s", role, e)

    return gen()


def set_exposure(us: float, role: str = "front") -> dict:
    r = _post_json("/exposure", {"exposure_us": float(us), "role": _norm_role(role)})
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "设置曝光失败"))
    return {k: r[k] for k in ("cur", "min", "max") if k in r}


def get_exposure(role: str = "front") -> dict:
    r = _get_json("/exposure", {"role": _norm_role(role)})
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "读取曝光失败"))
    return {k: r[k] for k in ("cur", "min", "max") if k in r}


def set_gain(db: float, role: str = "front") -> dict:
    r = _post_json("/gain", {"gain_db": float(db), "role": _norm_role(role)})
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "设置增益失败"))
    return {k: r[k] for k in ("cur", "min", "max") if k in r}


def get_gain(role: str = "front") -> dict:
    r = _get_json("/gain", {"role": _norm_role(role)})
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "读取增益失败"))
    return {k: r[k] for k in ("cur", "min", "max") if k in r}


def set_orient(role: str, mode: str) -> str:
    r = _post_json("/orient", {"side": _norm_role(role), "mode": mode})
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "设置方向失败"))
    return r.get("mode", mode)


def get_orient() -> dict:
    try:
        return _get_json("/orient").get("orient", {})
    except Exception:  # noqa: BLE001
        return {}


def pause():
    """暂停子进程抓帧并释放相机（把相机让给海康 MVS 客户端调参）。"""
    try:
        _post_json("/pause", {})
    except Exception as e:  # noqa: BLE001
        log.warning("pause 转发失败：%s", e)


def resume():
    """恢复子进程抓帧（要用相机时调）。"""
    try:
        _post_json("/resume", {})
    except Exception as e:  # noqa: BLE001
        log.warning("resume 转发失败：%s", e)


def close():
    """兼容旧接口：主进程不再直接持有相机。等价于让子进程释放相机（pause），供 MVS 接管。"""
    pause()


def sdk_available() -> bool:
    """子进程 SDK 是否就绪（供探测）。"""
    return bool(status().get("sdk"))
