"""自动识别循环：来料就绪 → 处理 → 标记完成 → 下一份，并向看板实时推送。

与来料方式解耦：处理逻辑通过 process_fn(job) 注入（server 传入"识别+质检+入库"函数），
来料源通过 feeder 注入（当前为 FolderWatchFeeder：监听相机输出目录）。本模块只管
循环、状态与订阅推送。（电机/弹仓/串口握手 已弃用）
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Callable, Optional

from .feeder import FeederController, Job


class PipelineRunner:
    """单实例自动流水线控制器：start/stop/status + SSE 订阅推送。"""

    def __init__(self):
        self._feeder: Optional[FeederController] = None
        self._process: Optional[Callable[[Job], dict]] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        # 状态
        self.count = 0
        self.last: Optional[dict] = None
        self.desc = ""
        self.started_at: Optional[float] = None

    # ---- 控制 ----
    def start(self, feeder: FeederController, process_fn: Callable[[Job], dict],
              desc: str = "") -> bool:
        if self._running:
            return False
        self._feeder = feeder
        self._process = process_fn
        self.desc = desc
        self.count = 0
        self.last = None
        self.started_at = time.time()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._feeder:                 # 唤醒可能阻塞在 wait_ready 的监听器
            try:
                self._feeder.close()
            except Exception:
                pass

    def status(self) -> dict:
        return {
            "running": self._running,
            "count": self.count,
            "desc": self.desc,
            "last": self.last,
        }

    # ---- SSE 订阅 ----
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _publish(self, event: dict) -> None:
        with self._lock:
            for q in list(self._subs):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

    def push_result(self, result: dict) -> None:
        """外部来源（如静止即拍 motion_trigger）把一条处理结果推给订阅者(工位屏/工作台)。"""
        self.count += 1
        self.last = result
        self._publish({"type": "result", "index": self.count, **result})

    def push_stage(self, stage: str, text: str = "", **extra) -> None:
        """推一条**进度**事件（不是最终结果）：让前端实时看到"拍照/识别/大模型/框图"走到哪一步。

        整条链路 5~15s，原先只在全部完成后推 result，中间毫无反馈、看不出是否在动。
        stage 取值见前端 STAGES：capture/recognize/inspect/annotate/done。
        """
        self._publish({"type": "stage", "stage": stage, "text": text, **extra})

    # ---- 主循环：来料就绪 → 处理 → 标记完成 → 下一份 ----
    def _loop(self) -> None:
        try:
            while self._running:
                job = self._feeder.wait_ready()        # 等下一份就绪
                if job is None:                        # 没有更多
                    break
                if not self._running:
                    break
                try:
                    result = self._process(job)        # 取图→识别质检→入库
                except Exception as e:                 # 单份失败不拖垮整条线
                    traceback.print_exc()
                    result = {"ok": False, "pos_id": job.pos_id, "error": str(e)}
                self.count += 1
                self.last = result
                self._publish({"type": "result", "index": self.count, **result})
                self._feeder.send_done(result)         # 标记该份处理完成
        finally:
            self._running = False
            if self._feeder:
                self._feeder.close()
            self._publish({"type": "stopped", "count": self.count})


# 全局单实例
runner = PipelineRunner()
