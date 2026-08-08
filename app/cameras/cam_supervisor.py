"""相机子进程监督器：在主服务进程里用 subprocess 拉起 cam_service，健康检查，卡死/退出即秒级重启。

主服务 lifespan startup 调 start()、shutdown 调 stop()。健康检查线程每隔几秒 ping 子进程 /status，
连续无响应(卡死)或进程已退出 → taskkill 旧进程、按退避重启。这样海康 SDK 无论怎么卡，都只连累
可被随时重启的小进程，主服务与网页永不需要人工重启整个系统。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request

from ..core import BASE_DIR

log = logging.getLogger("yxq.cam.sup")

CAM_PORT = int(os.environ.get("CAM_PORT", "8811"))
_HEALTH_URL = f"http://127.0.0.1:{CAM_PORT}/status"
_PING_SEC = float(os.environ.get("CAM_PING_SEC", "5") or 5)          # 健康检查间隔
_PING_TIMEOUT = float(os.environ.get("CAM_PING_TIMEOUT", "4") or 4)  # 单次 ping 超时
_MAX_FAILS = int(os.environ.get("CAM_PING_FAILS", "3") or 3)         # 连续失败几次判死
_WARMUP_SEC = float(os.environ.get("CAM_WARMUP_SEC", "20") or 20)    # 新进程启动宽限(开相机+首帧)


def _python() -> str:
    """优先用当前解释器（主服务由 .venv 启动 → 子进程同环境，拿得到 MVS/paddle 等依赖）。"""
    return sys.executable or os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")


class _Supervisor:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._run = False
        self._lock = threading.Lock()
        self._started_ts = 0.0
        self._restarts = 0

    # ---- 生命周期 ----
    def start(self):
        with self._lock:
            if self._run:
                return
            self._run = True
        self._spawn()
        self._thread = threading.Thread(target=self._watch, daemon=True, name="cam-supervisor")
        self._thread.start()
        log.info("相机子进程监督已启动（端口 %d）", CAM_PORT)

    def stop(self):
        with self._lock:
            self._run = False
        self._kill()
        log.info("相机子进程监督已停止，子进程已回收")

    # ---- 拉起 / 杀死 ----
    def _spawn(self):
        self._kill()                                   # 先清掉可能残留的旧实例
        env = dict(os.environ)
        env.setdefault("CAM_PORT", str(CAM_PORT))
        creation = 0
        if os.name == "nt":                            # 新进程组，便于整组结束、且不随控制台 Ctrl+C 连坐
            creation = subprocess.CREATE_NEW_PROCESS_GROUP
        self._proc = subprocess.Popen(
            [_python(), "-m", "app.cameras.cam_service"],
            cwd=BASE_DIR, env=env, creationflags=creation)
        self._started_ts = time.time()
        log.info("已拉起相机子进程 pid=%s", self._proc.pid)

    def _kill(self):
        p = self._proc
        self._proc = None
        if not p:
            return
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()                           # 卡死进程 terminate 无效 → 强杀
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception as e:  # noqa: BLE001
            log.warning("回收子进程失败 pid=%s：%s", getattr(p, "pid", "?"), e)

    def _ping(self) -> bool:
        try:
            with urllib.request.urlopen(_HEALTH_URL, timeout=_PING_TIMEOUT) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    # ---- 健康检查循环 ----
    def _watch(self):
        fails = 0
        while self._run:
            time.sleep(_PING_SEC)
            if not self._run:
                break
            p = self._proc
            # 进程已退出（含子进程自我 os._exit）→ 立即重启
            if p is None or p.poll() is not None:
                log.warning("相机子进程已退出（code=%s），重启", getattr(p, "returncode", None))
                self._restart()
                fails = 0
                continue
            # 启动宽限期内不判死（开相机+首帧需要时间）
            if time.time() - self._started_ts < _WARMUP_SEC:
                continue
            if self._ping():
                fails = 0
            else:
                fails += 1
                log.warning("相机子进程健康检查失败 %d/%d", fails, _MAX_FAILS)
                if fails >= _MAX_FAILS:                 # 连续无响应=卡死 → 杀掉重启
                    log.error("相机子进程连续无响应，判定卡死，强制重启")
                    self._restart()
                    fails = 0

    def _restart(self):
        self._restarts += 1
        backoff = min(10.0, 1.5 * self._restarts if self._restarts <= 3 else 5.0)
        self._spawn()
        time.sleep(backoff)                            # 退避，防端口未释放/死循环重启

    def status(self) -> dict:
        p = self._proc
        return {"running": self._run, "pid": getattr(p, "pid", None),
                "alive": bool(p and p.poll() is None), "restarts": self._restarts}


supervisor = _Supervisor()


def start():
    supervisor.start()


def stop():
    supervisor.stop()
