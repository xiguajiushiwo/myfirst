from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from PIL import Image, ImageStat

from .core import UPLOAD_DIR
from .logging_setup import get_logger
from .pipeline.feeder import Job


log = get_logger("yxq.remote_jobs")
TERMINAL_STATES = {"completed", "failed"}


class RemoteImageQualityError(ValueError):
    pass


class RemoteJobManager:
    def __init__(self, root: str | Path | None = None, processor: Callable | None = None):
        self.root = Path(root or Path(UPLOAD_DIR) / "remote_jobs")
        self.processor = processor
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[queue.Queue]] = {}

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            self._running = True
            self._recover_jobs()
            self._thread = threading.Thread(target=self._worker, daemon=True, name="remote-gpu-worker")
            self._thread.start()
            log.info("远程识别任务队列已启动 root=%s", self.root)

    def stop(self) -> None:
        with self._lock:
            self._running = False
        self._queue.put(None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def submit(self, *, job_id: str, front, back, station_id: str, operator: str = "",
               batch_id: int | None = None, mode: str = "geo", template_id: str | None = None,
               current_year: int | None = None, threshold: float | None = None,
               metadata: dict | None = None) -> tuple[dict, bool]:
        job_id = self._safe_job_id(job_id)
        existing = self.get(job_id)
        if existing:
            return existing, False
        job_dir = self.root / job_id
        incoming = self.root / f".{job_id}.{uuid.uuid4().hex}.tmp"
        incoming.mkdir(parents=True, exist_ok=False)
        try:
            front_info = self._store_image(front, incoming / "front.jpg")
            back_info = self._store_image(back, incoming / "back.jpg")
            self._validate_stored_images(incoming / "front.jpg", incoming / "back.jpg")
            now = self._now()
            state = {
                "job_id": job_id,
                "status": "queued",
                "stage": "queued",
                "message": "图片已接收，等待识别",
                "station_id": (station_id or "").strip()[:64],
                "operator": (operator or "").strip()[:64],
                "batch_id": int(batch_id) if batch_id else None,
                "mode": "rules" if mode == "rules" else "geo",
                "template_id": (template_id or "").strip() or None,
                "current_year": int(current_year) if current_year else None,
                "threshold": float(threshold) if threshold is not None else None,
                "metadata": metadata or {},
                "images": {"front": front_info, "back": back_info},
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": "",
            }
            self._write_json(incoming / "job.json", state)
            incoming.replace(job_dir)
        except Exception:
            shutil.rmtree(incoming, ignore_errors=True)
            raise
        self._queue.put(job_id)
        self._publish(job_id, {"type": "status", **state})
        return state, True

    def get(self, job_id: str) -> dict | None:
        try:
            path = self.root / self._safe_job_id(job_id) / "job.json"
        except ValueError:
            return None
        if not path.is_file():
            return None
        with self._lock:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None

    def retry(self, job_id: str) -> dict:
        state = self.get(job_id)
        if not state:
            raise KeyError(job_id)
        if state["status"] not in TERMINAL_STATES:
            return state
        state.update(status="queued", stage="queued", message="任务已重新排队", error="",
                     started_at=None, finished_at=None, result=None, updated_at=self._now())
        self._save(state)
        self._queue.put(state["job_id"])
        self._publish(state["job_id"], {"type": "status", **state})
        return state

    def subscribe(self, job_id: str) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.setdefault(job_id, []).append(channel)
        return channel

    def unsubscribe(self, job_id: str, channel: queue.Queue) -> None:
        with self._lock:
            subscribers = self._subscribers.get(job_id, [])
            if channel in subscribers:
                subscribers.remove(channel)
            if not subscribers:
                self._subscribers.pop(job_id, None)

    def queue_position(self, job_id: str) -> int:
        with self._queue.mutex:
            pending = [item for item in list(self._queue.queue) if item]
        try:
            return pending.index(job_id) + 1
        except ValueError:
            return 0

    def _worker(self) -> None:
        while self._running:
            job_id = self._queue.get()
            if job_id is None:
                break
            try:
                self._process(job_id)
            except Exception:
                log.exception("远程任务处理器异常 job_id=%s", job_id)
            finally:
                self._queue.task_done()

    def _process(self, job_id: str) -> None:
        state = self.get(job_id)
        if not state or state["status"] in TERMINAL_STATES:
            return
        state.update(status="processing", stage="recognize", message="服务器正在识别",
                     started_at=self._now(), updated_at=self._now())
        self._save(state)
        self._publish(job_id, {"type": "status", **state})
        try:
            processor = self.processor or self._default_processor
            result = processor(state, self._stage_callback(job_id))
            state.update(status="completed", stage="done", message="识别完成", result=result,
                         error="", finished_at=self._now(), updated_at=self._now())
        except Exception as exc:
            log.exception("远程识别失败 job_id=%s", job_id)
            state.update(status="failed", stage="failed", message="识别失败", error=str(exc),
                         finished_at=self._now(), updated_at=self._now())
        self._save(state)
        self._publish(job_id, {"type": "result", **state})

    def _default_processor(self, state: dict, on_stage: Callable) -> dict:
        from . import services

        job_dir = self.root / state["job_id"]
        job = Job(pos_id=state["job_id"], paths={
            "front": str(job_dir / "front.jpg"),
            "back": str(job_dir / "back.jpg"),
        }, meta={"source": "remote", "station_id": state["station_id"]})
        return services.analyze_and_save(
            job,
            operator=state["operator"],
            mode=state["mode"],
            template_id=state.get("template_id"),
            current_year=state.get("current_year"),
            threshold=state.get("threshold"),
            batch_id=state["batch_id"],
            save=True,
            on_stage=on_stage,
        )

    def _stage_callback(self, job_id: str) -> Callable:
        def callback(stage: str, text: str = "", **extra) -> None:
            state = self.get(job_id)
            if not state:
                return
            state.update(stage=stage, message=text or state.get("message", ""), updated_at=self._now())
            self._save(state)
            self._publish(job_id, {"type": "stage", "job_id": job_id, "status": state["status"],
                                   "stage": stage, "message": text, **extra})
        return callback

    def _recover_jobs(self) -> None:
        for path in self.root.glob("*/job.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if state.get("status") not in TERMINAL_STATES:
                state.update(status="queued", stage="queued", message="服务重启，任务已重新排队",
                             started_at=None, updated_at=self._now())
                self._write_json(path, state)
                self._queue.put(state["job_id"])

    def _save(self, state: dict) -> None:
        with self._lock:
            self._write_json(self.root / state["job_id"] / "job.json", state)

    def _publish(self, job_id: str, event: dict) -> None:
        with self._lock:
            for channel in list(self._subscribers.get(job_id, [])):
                try:
                    channel.put_nowait(event)
                except queue.Full:
                    pass

    @staticmethod
    def _store_image(upload, target: Path) -> dict:
        hasher = hashlib.sha256()
        size = 0
        with target.open("wb") as output:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise ValueError("单张图片不能超过 100MB")
                hasher.update(chunk)
                output.write(chunk)
        if not size:
            raise ValueError("上传图片为空")
        with Image.open(target) as image:
            image.verify()
        with Image.open(target) as image:
            width, height = image.size
            if width < 640 or height < 480:
                raise ValueError("图片分辨率过低")
            image_format = image.format
            image_mode = image.mode
            if image_format != "JPEG" or image_mode not in ("RGB", "L"):
                converted = image.convert("RGB")
                converted.save(target, format="JPEG", quality=95, subsampling=0)
        return {"name": target.name, "size": size, "sha256": hasher.hexdigest(),
                "width": width, "height": height}

    @classmethod
    def _validate_stored_images(cls, front: Path, back: Path) -> None:
        stats = {"front": cls._image_stats(front), "back": cls._image_stats(back)}
        dark = [
            side for side, item in stats.items()
            if item["mean"] < 8.0 and item["p99"] < 40.0
        ]
        if not dark:
            return
        labels = {"front": "正面", "back": "反面"}
        detail = "；".join(
            f"{labels.get(side, side)} mean={stats[side]['mean']:.1f} p99={stats[side]['p99']:.1f}"
            for side in dark
        )
        raise RemoteImageQualityError(
            f"{'、'.join(labels.get(side, side) for side in dark)}画面过暗，无法识别内存条。"
            f"请检查光源是否打开、曝光/增益是否过低，并在工作台实时画面确认图像清晰。{detail}"
        )

    @staticmethod
    def _image_stats(path: Path) -> dict:
        with Image.open(path) as image:
            gray = image.convert("L")
            max_side = max(gray.size)
            if max_side > 1200:
                scale = 1200 / max_side
                gray = gray.resize((max(1, int(gray.width * scale)), max(1, int(gray.height * scale))))
            hist = gray.histogram()
            total = sum(hist)
            cutoff = max(1, int(total * 0.99))
            acc = 0
            p99 = 0
            for value, count in enumerate(hist):
                acc += count
                if acc >= cutoff:
                    p99 = value
                    break
            return {"mean": float(ImageStat.Stat(gray).mean[0]), "p99": float(p99)}

    @staticmethod
    def _safe_job_id(job_id: str) -> str:
        value = (job_id or "").strip()
        if not value or len(value) > 80 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in value):
            raise ValueError("job_id 只能包含字母、数字、短横线和下划线")
        return value

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")


manager = RemoteJobManager()
