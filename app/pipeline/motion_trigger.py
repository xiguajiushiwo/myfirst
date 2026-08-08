"""静止即拍：盯相机预览帧，"放盘 → 画面静止"就自动触发拍照识别（纯软件，无需硬件触发）。

状态机（防连拍/防手还在画面里）：
    待机 →(检测到明显运动:放盘/手进入)→ 等待静止 →(连续N帧静止)→ 触发 process_fn()
          → 已拍锁定 →(再次检测到运动:取盘/换盘)→ 回到待机 重新武装
关键：拍完进入"已拍锁定"，必须先看到**取盘的运动**才重新武装，否则同一盘会被反复拍。

帧差 = 相邻两帧（灰度下采样后）平均绝对差(0~255)。阈值/帧数/冷却都可 .env 调，现场标定。
不改动手动拍照——这是**额外**的自动触发通道；结果推给现有 SSE（工位屏/工作台可见）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from typing import Callable, Optional

log = logging.getLogger("yxq.motion")

# ---- 参数（.env 可覆盖，现场标定）----
_POLL = float(os.environ.get("AUTO_POLL", "0.2"))            # 抓预览帧间隔(秒)
# 现场实测(俯视紧贴托盘)：静态噪声~0.74 / 挥手~1.5 / 放取托盘峰值~10、多在1~3.6。
# 故运动阈值取 2.5(高于噪声+挥手、放盘可靠越过)、静止阈值 1.5(高于噪声，放稳即判静止)。
_MOTION_THR = float(os.environ.get("AUTO_MOTION_THR", "2.5"))  # 帧差 > 此 = 有明显运动
_STILL_THR = float(os.environ.get("AUTO_STILL_THR", "1.5"))    # 帧差 < 此 = 静止
_STILL_FRAMES = int(os.environ.get("AUTO_STILL_FRAMES", "4"))  # 连续静止帧数达标才拍
_COOLDOWN = float(os.environ.get("AUTO_COOLDOWN", "1.5"))    # 拍后冷却(秒)
# 换盘判据：与"拍照时刻"基准帧的整场差异 > 此值 = 这盘已被换掉 → 重新武装。
# 必须高于底噪(约1.7)、低于"换一盘"的真实差异(不同条的芯片/丝印位置差异远大于此)。
_SCENE_THR = float(os.environ.get("AUTO_SCENE_THR", "3.5"))
_DOWN_W = 160                                                 # 帧差计算下采样宽度


def _gray_small(arr):
    import cv2
    h, w = arr.shape[:2]
    g = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY) if arr.ndim == 3 else arr
    if w > _DOWN_W:
        g = cv2.resize(g, (_DOWN_W, max(1, int(h * _DOWN_W / w))))
    return g


class MotionTrigger:
    """单实例静止即拍控制器：start(process_fn) / stop / status。盯 front 相机预览帧。"""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._process: Optional[Callable[[], dict]] = None
        # 空盘防护：返回 False 表示画面里没有内存条 → 不拍（省一次拍照+识别+大模型调用）
        self._presence: Optional[Callable[[], bool]] = None
        self.skipped_empty = 0        # 因空盘而跳过的次数(诊断用)
        self.role = "front"
        self.state = "idle"           # idle / arming(等静止) / locked(已拍待取盘)
        self.count = 0
        self.last: Optional[dict] = None
        self.desc = ""
        self.last_diff = 0.0          # 最近一次帧差(诊断/标阈值用)
        self.peak_diff = 0.0          # 近窗口内峰值帧差
        self.frames = 0               # 已处理帧数(判断是否真在取帧)
        self.still = 0                # 当前连续静止帧数(前端诊断"卡在等静止"用)
        self._fired_ref = None        # 拍照时刻的基准帧：locked 下与它比对，发现换盘(识别盲区内换的也能认出)
        self.scene_diff = 0.0         # 当前帧与基准帧的差异(诊断"换盘识别"用)
        self.noise = 0.0              # 近期底噪估计(帧差中位数附近的低位值)
        self._recent: list = []       # 近 50 帧帧差窗口(估底噪用)

    def start(self, process_fn: Callable[[], dict], role: str = "front", desc: str = "",
              presence_fn: Optional[Callable[[], bool]] = None) -> bool:
        if self._running:
            return False
        self._process = process_fn
        self._presence = presence_fn
        self.role = role
        self.desc = desc
        self.state = "idle"
        self.count = 0
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("静止即拍已启动：%s", desc)
        return True

    def stop(self) -> None:
        self._running = False
        log.info("静止即拍已停止")

    def status(self) -> dict:
        return {"running": self._running, "state": self.state,
                "count": self.count, "last": self.last, "desc": self.desc,
                "frames": self.frames,
                "last_diff": round(self.last_diff, 2), "peak_diff": round(self.peak_diff, 2),
                "still": self.still, "noise": round(self.noise, 2),
                "skipped_empty": self.skipped_empty,
                "scene_diff": round(self.scene_diff, 2),
                "blocked": self._blocked_reason(),
                "params": {"motion": _MOTION_THR, "still": _STILL_THR,
                           "still_frames": _STILL_FRAMES, "cooldown": _COOLDOWN,
                           "scene": _SCENE_THR}}

    def _blocked_reason(self) -> str:
        """诊断：为什么没触发。空串=正常。给前端直接显示，免得只能看数字猜。"""
        if not self._running:
            return ""
        if self.frames < 3:
            return "取帧中，等首帧比对"
        if self.noise and self.noise >= _STILL_THR:
            return (f"底噪 {self.noise:.2f} ≥ 静止阈值 {_STILL_THR}"
                    f"：永远判不到静止，需把 AUTO_STILL_THR 调到底噪之上(建议 {self.noise * 1.55:.1f})")
        if self.noise and self.noise >= _MOTION_THR:
            return (f"底噪 {self.noise:.2f} ≥ 运动阈值 {_MOTION_THR}"
                    f"：会被误判成一直在动，需调高 AUTO_MOTION_THR")
        if self.state == "idle":
            return f"等放盘：帧差需 > {_MOTION_THR} 才开始武装"
        if self.state == "arming":
            return f"等静止：需连续 {_STILL_FRAMES} 帧帧差 < {_STILL_THR}（当前已连续 {self.still} 帧）"
        if self.state == "locked":
            if self.count == 0 and self.skipped_empty:
                return (f"上次静止时画面无内存条(空盘)，已跳过不拍 ×{self.skipped_empty}；"
                        f"放盘后帧差 > {_MOTION_THR} 会重新武装")
            return (f"本盘已拍完，等换盘：帧差 > {_MOTION_THR} 或换盘差异 > {_SCENE_THR}"
                    f"（当前换盘差异 {self.scene_diff:.2f}）即自动检下一盘")
        return ""

    def _loop(self) -> None:
        from ..cameras import hik_camera as hik
        prev = None
        still = 0
        try:
            cam = hik.get_camera(self.role)
        except Exception as e:  # noqa: BLE001
            log.warning("静止即拍取相机失败：%s", e)
            self._running = False
            return
        fails = 0
        while self._running:
            try:
                arr = cam.grab_array(timeout_ms=1000)
                g = _gray_small(arr)
                fails = 0
            except Exception:
                fails += 1
                if fails % 8 == 0:                 # 连续~1.6s 取不到帧=卡流 → 重连一次(不再干等十几秒)
                    log.warning("静止即拍取帧连续失败%d次，重连相机", fails)
                    try:
                        cam._recover()
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(_POLL)
                continue
            if prev is None:
                prev = g
                time.sleep(_POLL)
                continue
            try:
                import numpy as np
                diff = float(np.abs(g.astype("int16") - prev.astype("int16")).mean())
            except Exception:
                diff = 0.0
            prev = g
            self.last_diff = diff
            self.peak_diff = max(self.peak_diff * 0.9, diff)   # 衰减峰值，便于观察瞬时运动
            self.frames += 1
            # 底噪估计：近 50 帧取第 25 百分位(避开运动峰值)，用于诊断"阈值是否低于底噪"
            self._recent.append(diff)
            if len(self._recent) > 50:
                self._recent.pop(0)
            if len(self._recent) >= 10:
                s = sorted(self._recent)
                self.noise = s[len(s) // 4]

            if self.state == "idle":
                if diff > _MOTION_THR:                 # 有人放盘/手进入 → 开始等静止
                    self.state = "arming"
                    still = 0
            elif self.state == "arming":
                if diff < _STILL_THR:
                    still += 1
                    if still >= _STILL_FRAMES:         # 连续静止达标 → 先确认盘里有条，再拍
                        if self._has_stick():
                            self._fired_ref = g        # 记下"这一盘拍照时"的画面，供 locked 比对换盘
                            self._fire()
                            time.sleep(_COOLDOWN)
                        else:
                            # 空盘/取盘后的空场：静止也不拍，避免无效记录与白花大模型调用。
                            # 同样进 locked：等下次真放盘的运动再武装，不然会每 4 帧重复检一次。
                            self.skipped_empty += 1
                            log.info("静止但画面无内存条(空盘)，跳过本次触发")
                        self.state = "locked"
                        prev = None                    # 重置基准，避免拍照/抖动误判
                        still = 0
                else:
                    still = 0                          # 又动了，重新计静止
            elif self.state == "locked":
                # 重新武装有两条路，缺一不可：
                #  ① 瞬时运动：看到取盘/放盘的动作(操作慢、动作被循环抓到时走这条)
                #  ② 画面已换：与"拍照时刻"的基准帧比差异大 → 盘被换过。
                #     必需——识别要 5~15s，这期间循环阻塞不抓帧，取盘+放下一盘的动作
                #     可能整个落在盲区里没被看见；只靠 ① 会永远等不到运动而卡死在 locked。
                self.scene_diff = self._scene_change(g)
                if diff > _MOTION_THR or self.scene_diff > _SCENE_THR:
                    self.state = "idle"
                    self._fired_ref = None
                    self.scene_diff = 0.0
            self.still = still                         # 同步给前端诊断(看卡在第几帧)
            time.sleep(_POLL)
        self.state = "idle"

    def _scene_change(self, g) -> float:
        """当前帧与"拍照时刻"基准帧的平均绝对差：判断这盘是不是已经被换掉了。

        用整场差异而非相邻帧差——识别期间循环阻塞，换盘动作看不到，但换完的**画面**是不一样的。
        """
        if self._fired_ref is None:
            return 0.0
        try:
            import numpy as np
            if g.shape != self._fired_ref.shape:
                return 0.0
            return float(np.abs(g.astype("int16") - self._fired_ref.astype("int16")).mean())
        except Exception:  # noqa: BLE001
            return 0.0

    def _has_stick(self) -> bool:
        """画面里有内存条吗？没配 presence_fn 或检测异常 → 保守返回 True(宁可拍，绝不漏真盘)。"""
        if not self._presence:
            return True
        try:
            return bool(self._presence())
        except Exception as e:  # noqa: BLE001
            log.warning("空盘检测失败(按有条处理)：%s", e)
            return True

    def _fire(self) -> None:
        """触发一次：调 process_fn（拍+识别+入库），结果推给 SSE。异常不拖垮循环。"""
        if not self._process:
            return
        try:
            result = self._process()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            result = {"ok": False, "error": str(e)}
        self.count += 1
        self.last = result
        try:
            from .runner import runner as pipeline_runner
            pipeline_runner.push_result(result)        # 复用现有 SSE：工位屏/工作台可见
        except Exception:  # noqa: BLE001
            pass


# 全局单实例
motion_trigger = MotionTrigger()
