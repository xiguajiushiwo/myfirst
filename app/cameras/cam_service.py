"""相机子进程服务：**独占**海康 MVS SDK，把易卡死的相机操作关进一个可被主服务秒级重启的小进程。

为什么要它：SDK 某次调用在坏会话下不按 timeout 返回、卡死在 native 层时，会把持锁线程永久堵死
（表现为"HTTP 200 但 0 帧"），过去只能重启整个主服务。把 SDK 隔离到本子进程后：子进程卡死 →
主服务看门狗(cam_supervisor) 直接 taskkill 重启本进程，主服务/网页无感、永不需要人工重启整个系统。

架构要点：
- **每台相机一个后台 grab 线程**持续抓帧写入 per-role 缓冲；预览/取帧/双拍**全部从缓冲读**，
  绝不在请求线程里直接调 SDK 取流 → 请求永不阻塞在 SDK。唯一碰 SDK 取流的是这个后台线程，
  它若卡死 → 本进程整体被主服务杀掉重启。
- 曝光/增益/方向等控制命令仍走 SDK（在本进程内），与 grab 串行化（沿用 hik_sdk 的锁），
  最多等一帧；真卡死也只连累本子进程，被重启。
- 只绑 127.0.0.1:${CAM_PORT:-8811}，不对外。独立日志 logs/camera.log（避开主进程滚动日志多进程写冲突）。

启动：`python -m app.cameras.cam_service`（由 cam_supervisor 在主服务里拉起+监督）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler

from ..core import BASE_DIR
from ..env_loader import load_project_env

load_project_env(BASE_DIR)

from . import hik_sdk as sdk

log = logging.getLogger("yxq.cam")

_CAMERA_MODE = os.environ.get("HIK_CAMERA_MODE", "dual").strip().lower()
_SINGLE_CAMERA = _CAMERA_MODE == "single"
_ROLES = () if _CAMERA_MODE == "uvc" else (("front",) if _SINGLE_CAMERA else ("front", "back"))
CAM_PORT = int(os.environ.get("CAM_PORT", "8811"))
# 后台 grab 目标帧率（缓冲刷新率）。抓拍/双拍取缓冲最新帧，≤ 1/该值 秒足够新。
_GRAB_FPS = float(os.environ.get("CAM_GRAB_FPS", "12") or 12)
# 缓冲多久没刷新算"这路相机卡了"（供 /status 与看门狗判活）
_STALE_SEC = float(os.environ.get("CAM_STALE_SEC", "6") or 6)
# 连续 grab 失败达此数 → 本进程退出，交给主服务重启（比在进程内反复 _recover 更干脆）
_DIE_AFTER_FAILS = int(os.environ.get("CAM_DIE_AFTER_FAILS", "60") or 60)


def _setup_log():
    """独立日志：logs/camera.log。用 RotatingFileHandler（单进程独占，无多进程滚动冲突）。"""
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    fh = RotatingFileHandler(os.path.join(BASE_DIR, "logs", "camera.log"),
                             maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.handlers = [fh, ch]


class _Grabber:
    """单角色后台抓帧线程：持续 grab_array → 存 per-role 最新帧缓冲（已做方向校正）。

    预览/取帧/双拍都从这里读 latest，绝不自己调 SDK 取流。连续失败会 _recover；
    长时间无法恢复 → 置 dead，由看门狗决定退出进程。
    """

    def __init__(self, role: str):
        self.role = role
        self._arr = None                 # 最新一帧 BGR np 数组（已方向校正）
        self._ts = 0.0                   # 最新帧时间戳
        self._lock = threading.Lock()
        self._fails = 0                  # 连续失败计数
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"grab-{role}")

    def start(self):
        self._thread.start()

    def stop(self):
        self._run = False

    @property
    def fresh(self) -> bool:
        return self._ts > 0 and (time.time() - self._ts) < _STALE_SEC

    @property
    def fails(self) -> int:
        return self._fails

    def latest(self):
        """返回 (arr.copy(), ts)；还没抓到过则 (None, 0)。"""
        with self._lock:
            if self._arr is None:
                return None, 0.0
            return self._arr.copy(), self._ts

    def _loop(self):
        period = 1.0 / max(1.0, _GRAB_FPS)
        next_grab = time.monotonic()
        cam = None
        while self._run:
            # 暂停(把相机让给 MVS 调参)：关闭句柄释放相机，空转等 resume。清空计数避免误触发看门狗。
            if _paused.is_set():
                if cam is not None:
                    try:
                        cam.close()
                    except Exception:  # noqa: BLE001
                        pass
                    cam = None
                self._fails = 0
                self._ts = 0.0
                time.sleep(0.3)
                continue
            try:
                if cam is None:
                    cam = sdk.get_camera(self.role)     # 首次/重连后取实例（内部按 SN 打开取流）
                arr = cam.grab_array(timeout_ms=1500)
                with self._lock:
                    self._arr = arr
                    self._ts = time.time()
                self._fails = 0
                next_grab += period
                delay = next_grab - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_grab = time.monotonic()
            except Exception as e:  # noqa: BLE001
                self._fails += 1
                if self._fails == 1 or self._fails % 10 == 0:
                    log.warning("grab 失败 role=%s 第%d次：%s", self.role, self._fails, e)
                # 连续失败到一定程度先在进程内自愈一次（掉线/卡流→关句柄重开）
                if self._fails % 15 == 0 and cam is not None:
                    try:
                        cam._recover()
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(0.2)


_grabbers: dict[str, _Grabber] = {}
# 暂停标志：置位时后台 grab 线程关闭句柄释放相机，让海康 MVS 客户端能独占接管（调参用）。
_paused = threading.Event()


def _start_grabbers():
    for r in _ROLES:
        g = _Grabber(r)
        _grabbers[r] = g
        g.start()
    log.info("后台抓帧线程已启动：%s", ", ".join(_ROLES))


def _watchdog_loop():
    """进程级看门狗：任一路 grab 连续失败超阈值 → 退出进程，让主服务重启（比进程内死磕更可靠）。"""
    while True:
        time.sleep(3)
        if _paused.is_set():          # 已让给 MVS：不判死、不重启
            continue
        for r, g in _grabbers.items():
            if g.fails >= _DIE_AFTER_FAILS:
                log.error("role=%s 连续 grab 失败 %d 次，进程退出交由主服务重启", r, g.fails)
                os._exit(3)     # 硬退出：确保 native 卡死也能死掉，supervisor 会拉起新进程


# --------------------- FastAPI 应用（仅本机）---------------------

def _encode_jpeg(arr, quality: int) -> bytes:
    import cv2
    ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return buf.tobytes()


def _role(side: str) -> str:
    return side if side in _ROLES else "front"


def build_app():
    from fastapi import FastAPI, Form
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    app = FastAPI(title="相机子进程", version="1.0")

    @app.get("/status")
    def status():
        try:
            st = sdk.status()
        except Exception as e:  # noqa: BLE001
            st = {"sdk": False, "error": str(e), "devices": []}
        st["grab"] = {r: {"fresh": g.fresh, "fails": g.fails,
                          "age": round(time.time() - g._ts, 2) if g._ts else None}
                      for r, g in _grabbers.items()}
        st["active_roles"] = list(_ROLES)
        return st

    @app.get("/frame")
    def frame(role: str = "front", quality: int = 80):
        g = _grabbers[_role(role)]
        arr, ts = g.latest()
        if arr is None:
            return JSONResponse({"error": "暂无帧（相机预热中或已卡）"}, status_code=503)
        age_ms = max(0, round((time.time() - ts) * 1000))
        return Response(
            content=_encode_jpeg(arr, quality),
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Frame-Timestamp": f"{ts:.6f}",
                "X-Frame-Age-Ms": str(age_ms),
            },
        )

    @app.get("/preview")
    def preview(role: str = "front", quality: int = 70, max_fps: int = 0):
        r = _role(role)
        g = _grabbers[r]
        fps = max_fps or int(os.environ.get("HIK_PREVIEW_FPS", "8") or 8)
        period = 1.0 / max(1, fps)
        pw = int(os.environ.get("HIK_PREVIEW_W", "0") or 0)

        def gen():
            import cv2
            miss = 0
            last_ts = 0.0
            next_send = time.monotonic()
            while True:
                arr, ts = g.latest()
                if arr is None:
                    miss += 1
                    if miss > 40:                    # ~没帧太久，结束这路让前端 <img> onerror 重连
                        break
                    time.sleep(0.15)
                    continue
                miss = 0
                if ts <= last_ts:
                    time.sleep(min(period / 4, 0.01))
                    continue
                delay = next_send - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                    continue
                if pw and arr.shape[1] > pw:
                    nh = int(arr.shape[0] * pw / arr.shape[1])
                    arr = cv2.resize(arr, (pw, nh))
                jpg = _encode_jpeg(arr, quality)
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(jpg)).encode()
                       + b"\r\nX-Frame-Timestamp: " + f"{ts:.6f}".encode()
                       + b"\r\n\r\n" + jpg + b"\r\n")
                last_ts = ts
                next_send = max(next_send + period, time.monotonic())

        return StreamingResponse(
            gen(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/snapshot")
    def snapshot(path: str = Form(...), role: str = Form("front"), quality: int = Form(90)):
        """抓某角色最新缓冲帧存 JPG 到 path（与主进程同机，路径直接可写）。"""
        g = _grabbers[_role(role)]
        arr, ts = g.latest()
        if arr is None:
            return JSONResponse({"ok": False, "error": "暂无帧（相机预热中或已卡）"}, status_code=503)
        with open(path, "wb") as f:
            f.write(_encode_jpeg(arr, quality))
        return {"ok": True, "path": path, "age": round(time.time() - ts, 3)}

    @app.post("/capture_both")
    def capture_both(front_path: str = Form(...), back_path: str = Form(...), quality: int = Form(90)):
        """同时取 上(front)/下(back) 最新缓冲帧各存一张。两路缓冲都近实时→正反不错位。"""
        if _SINGLE_CAMERA:
            return JSONResponse({"ok": False, "error": "当前为单相机模式，请先拍正面，翻面后再拍反面"},
                                status_code=400)
        out = {}
        for role, p in (("front", front_path), ("back", back_path)):
            arr, ts = _grabbers[role].latest()
            if arr is None:
                return JSONResponse({"ok": False, "error": f"{role} 暂无帧"}, status_code=503)
            with open(p, "wb") as f:
                f.write(_encode_jpeg(arr, quality))
            out[role] = p
        return {"ok": True, **out}

    @app.get("/exposure")
    def get_exposure(role: str = "front"):
        try:
            return {"ok": True, "side": _role(role), **sdk.get_exposure(role=_role(role))}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

    @app.post("/exposure")
    def set_exposure(exposure_us: float = Form(...), role: str = Form("front")):
        try:
            return {"ok": True, "side": _role(role), **sdk.set_exposure(exposure_us, role=_role(role))}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

    @app.get("/gain")
    def get_gain(role: str = "front"):
        try:
            return {"ok": True, "side": _role(role), **sdk.get_gain(role=_role(role))}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

    @app.post("/gain")
    def set_gain(gain_db: float = Form(...), role: str = Form("front")):
        try:
            return {"ok": True, "side": _role(role), **sdk.set_gain(gain_db, role=_role(role))}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

    @app.get("/orient")
    def get_orient():
        return {"ok": True, "orient": sdk.get_orient()}

    @app.post("/orient")
    def set_orient(side: str = Form(...), mode: str = Form(...)):
        try:
            return {"ok": True, "side": side, "mode": sdk.set_orient(side, mode)}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

    @app.post("/pause")
    def pause():
        _paused.set()                # 后台 grab 线程见状即 close 释放相机，让 MVS 独占
        sdk.pause()
        return {"ok": True}

    @app.post("/resume")
    def resume():
        _paused.clear()
        sdk.resume()
        return {"ok": True}

    return app


def main():
    _setup_log()
    log.info("相机子进程启动 → 127.0.0.1:%d", CAM_PORT)
    _start_grabbers()
    threading.Thread(target=_watchdog_loop, daemon=True, name="cam-watchdog").start()
    import uvicorn
    uvicorn.run(build_app(), host="127.0.0.1", port=CAM_PORT, log_level="warning")


if __name__ == "__main__":
    main()
