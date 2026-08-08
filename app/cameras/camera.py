"""服务器端双相机采集（OpenCV / UVC）。

- 相机插在**服务器**上，后端直接读本机摄像头（正面 + 背面两台，按设备序号）。
- 每台相机一个常驻抓帧线程，保存"最新一帧"；预览(MJPEG)与拍照都从最新帧取，互不打架。
- 相机缺失/打不开时**优雅降级**（status 标 false），不影响其它功能与手动上传。

配置（.env）：
  FRONT_CAM_INDEX / BACK_CAM_INDEX  正/背面相机设备序号（默认 0 / 1）
  CAM_WIDTH / CAM_HEIGHT            采集分辨率（默认 1920x1080，日期小字建议拉高）
  CAM_PREVIEW_WIDTH                 预览缩放宽度（默认 960，省带宽；拍照仍用全分辨率）
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import cv2

_IDX = {
    "front": int(os.environ.get("FRONT_CAM_INDEX", "0")),
    "back": int(os.environ.get("BACK_CAM_INDEX", "1")),
}
_W = int(os.environ.get("CAM_WIDTH", "1920"))
_H = int(os.environ.get("CAM_HEIGHT", "1080"))
_PREVIEW_W = int(os.environ.get("CAM_PREVIEW_WIDTH", "960"))


class _Cam:
    def __init__(self, index: int):
        self.index = index
        self.cap = None
        self.frame = None
        self.lock = threading.Lock()
        self.run = False
        self.ok = False

    def open(self) -> bool:
        try:
            # Windows UVC 走 DirectShow 后端最稳
            cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
            if not cap or not cap.isOpened():
                if cap:
                    cap.release()
                return False
            # 关键：让相机用 MJPG 压缩输出，否则 4K 生数据(YUY2)会带宽不足→延迟+撕裂形变
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FPS, 30)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, _W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _H)
            # 只留最新一帧，避免读到堆积的旧帧（降低延迟）
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            self.cap = cap
            self.run = True
            self.ok = True
            threading.Thread(target=self._loop, daemon=True).start()
            return True
        except Exception:
            return False

    def _loop(self):
        while self.run:
            try:
                r, f = self.cap.read()
            except Exception:
                r, f = False, None
            if r and f is not None:
                with self.lock:
                    self.frame = f
            else:
                time.sleep(0.05)

    def latest(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()


_cams: dict[str, _Cam] = {}
_lock = threading.Lock()


def _get(side: str) -> Optional[_Cam]:
    """懒打开某一路相机；打不开返回 None。"""
    if side not in _IDX:
        return None
    with _lock:
        cam = _cams.get(side)
        if cam is None:
            cam = _Cam(_IDX[side])
            cam.open()
            _cams[side] = cam
        return cam if cam.ok else None


def status() -> dict:
    """返回各路相机是否可用（会尝试打开；首帧未就绪时最多等约 1.5 秒）。"""
    out = {}
    for side in _IDX:
        cam = _get(side)
        ready = False
        if cam and cam.ok:
            for _ in range(30):        # 4K 首帧可能慢，给一点时间
                if cam.latest() is not None:
                    ready = True
                    break
                time.sleep(0.05)
        out[side] = ready
    return out


def snapshot(side: str, out_path: str) -> bool:
    """抓当前最新一帧存到 out_path（全分辨率）。成功返回 True。"""
    cam = _get(side)
    if not cam:
        return False
    # 给抓帧线程一点时间拿到帧
    for _ in range(20):
        f = cam.latest()
        if f is not None:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            return bool(cv2.imwrite(out_path, f))
        time.sleep(0.05)
    return False


def mjpeg(side: str):
    """MJPEG 预览流生成器（缩放到 CAM_PREVIEW_WIDTH，省带宽）。"""
    cam = _get(side)
    boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
    while True:
        f = cam.latest() if cam else None
        if f is None:
            time.sleep(0.08)
            continue
        h, w = f.shape[:2]
        if w > _PREVIEW_W:
            f = cv2.resize(f, (_PREVIEW_W, int(h * _PREVIEW_W / w)))
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            yield boundary + buf.tobytes() + b"\r\n"
        time.sleep(0.06)
