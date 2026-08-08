"""相机输出目录监听（托盘拍照 → 写文件夹 → 自动识别）。

托盘方式：操作员放一盘(4根固定位)→拍照→相机把该盘照片写进 watch_dir 下一个新子文件夹；
本模块持续轮询，发现**新出现且图片写完**的子文件夹就返回，交给流水线自动识别入库。

> 电机传送/弹仓/到位信号双向握手 方案已全部弃用；不再有 SimulatedFeeder / SerialFeeder。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Job:
    """一次待处理事件：一盘/一根内存条 + 它的图片。"""
    pos_id: str                                  # 组标识（=子文件夹名）
    paths: dict                                  # {slot: 源图片路径} 如 {"front":..,"back":..}
    meta: dict = field(default_factory=dict)


class FeederController:
    """来料信号源接口。子类实现"等下一份到位"。"""

    def wait_ready(self) -> Optional[Job]:
        """阻塞直到下一份就绪，返回 Job；没有更多则返回 None。"""
        raise NotImplementedError

    def send_done(self, result: dict) -> None:
        """该份处理完成的回调（如落 .done 标记）。"""

    def close(self) -> None:
        pass


class FolderWatchFeeder(FeederController):
    """监听相机输出目录：每盘/每根 = 一个新子文件夹（含正反两张）。

    相机拍完自动在 watch_dir 下生成子文件夹并写入图片；本类持续轮询，发现
    **新出现且图片都写完** 的子文件夹就返回它，自动进入识别。已处理的子文件夹落
    `.done` 标记，重启不重复处理。

    process_existing=False：启动时把现有子文件夹标记为"已见"，只处理之后新增的。
    """

    def __init__(self, watch_dir: str, resolve_fn: Callable[[str], dict],
                 poll: float = 1.0, process_existing: bool = False):
        self.dir = watch_dir
        self.resolve = resolve_fn
        self.poll = max(0.3, poll)
        self.seen: set[str] = set()
        self._running = True
        os.makedirs(watch_dir, exist_ok=True)
        if not process_existing:
            for name in os.listdir(watch_dir):
                if os.path.isdir(os.path.join(watch_dir, name)):
                    self.seen.add(name)

    def _stable(self, slots: dict) -> bool:
        """两张图两次(间隔 poll)大小不变 且 可解码 → 判定写完了。"""
        try:
            sizes = {s: os.path.getsize(p) for s, p in slots.items()}
        except OSError:
            return False
        time.sleep(self.poll)
        for s, p in slots.items():
            try:
                if os.path.getsize(p) != sizes[s] or os.path.getsize(p) == 0:
                    return False
            except OSError:
                return False
        from PIL import Image
        for p in slots.values():
            try:
                Image.open(p).verify()
            except Exception:
                return False
        return True

    def _next_ready(self):
        try:
            names = os.listdir(self.dir)
        except OSError:
            return None
        # 按创建时间从早到晚，保证不漏（先到先处理）
        dirs = [(n, os.path.join(self.dir, n)) for n in names
                if os.path.isdir(os.path.join(self.dir, n))]
        dirs.sort(key=lambda x: os.path.getmtime(x[1]))
        for name, d in dirs:
            if name in self.seen:
                continue
            if os.path.exists(os.path.join(d, ".done")):
                self.seen.add(name)
                continue
            slots = self.resolve(d)
            if not (slots.get("front") and slots.get("back")):   # 需正反两张都在
                continue
            if self._stable(slots):
                return name, slots
        return None

    def wait_ready(self) -> Optional[Job]:
        while self._running:
            r = self._next_ready()
            if r:
                name, slots = r
                self.seen.add(name)
                return Job(pos_id=name, paths=dict(slots), meta={"source": "watch"})
            time.sleep(self.poll)
        return None

    def send_done(self, result: dict) -> None:
        # 落 .done 标记，重启后不再重复处理
        name = result.get("pos_id", "")
        try:
            open(os.path.join(self.dir, name, ".done"), "w").close()
        except Exception:
            pass

    def close(self) -> None:
        self._running = False
