from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import uuid
from pathlib import Path

import requests


class QCClient:
    def __init__(self, config: dict):
        self.server_url = config["server_url"].rstrip("/")
        self.api_key = config["api_key"]
        self.station_id = config["station_id"]
        self.cache_dir = Path(config.get("cache_dir") or "client_data")
        self.timeout = int(config.get("timeout_seconds") or 180)
        for name in ("pending", "confirmed", "failed"):
            (self.cache_dir / name).mkdir(parents=True, exist_ok=True)

    @property
    def headers(self) -> dict:
        return {"X-Client-Key": self.api_key}

    def health(self) -> dict:
        response = requests.get(f"{self.server_url}/api/remote/health", headers=self.headers, timeout=10)
        response.raise_for_status()
        return response.json()

    def capture(self, *, batch_id: int | None, operator: str, mode: str = "geo",
                template_id: str | None = None, current_year: int | None = None,
                threshold: float | None = None) -> Path:
        from app.cameras import hik_camera as hik
        from app.cameras import cam_supervisor

        job_id = f"{self.station_id}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        task_dir = self.cache_dir / "pending" / job_id
        task_dir.mkdir(parents=True, exist_ok=False)
        front = task_dir / "front.jpg"
        back = task_dir / "back.jpg"
        owns_camera_service = not hik.status().get("sdk")
        if owns_camera_service:
            cam_supervisor.start()
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if hik.status().get("sdk"):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("海康相机服务启动超时")
            hik.capture_both(str(front), str(back), quality=95)
            self._validate_capture_images(front, back)
        finally:
            if owns_camera_service:
                cam_supervisor.stop()
        payload = {
            "job_id": job_id,
            "station_id": self.station_id,
            "operator": operator,
            "batch_id": batch_id,
            "mode": mode,
            "template_id": template_id,
            "current_year": current_year,
            "threshold": threshold,
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (task_dir / "task.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return task_dir

    def _validate_capture_images(self, front: Path, back: Path) -> None:
        stats = {"front": self._capture_image_stats(front), "back": self._capture_image_stats(back)}
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
        raise RuntimeError(
            f"{'、'.join(labels.get(side, side) for side in dark)}画面过暗，无法识别内存条。"
            f"请检查光源是否打开、曝光/增益是否过低，并在相机调试页确认实时画面清晰。{detail}"
        )

    @staticmethod
    def _capture_image_stats(path: Path) -> dict:
        import cv2
        import numpy as np

        gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            raise RuntimeError(f"无法读取拍照图片：{path.name}")
        if max(gray.shape[:2]) > 1200:
            scale = 1200 / max(gray.shape[:2])
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        return {
            "mean": float(gray.mean()),
            "p99": float(np.percentile(gray, 99)),
        }

    def queue_files(self, front: str, back: str, *, batch_id: int | None,
                    operator: str, mode: str = "geo") -> Path:
        job_id = f"{self.station_id}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        task_dir = self.cache_dir / "pending" / job_id
        task_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(front, task_dir / "front.jpg")
        shutil.copy2(back, task_dir / "back.jpg")
        payload = {"job_id": job_id, "station_id": self.station_id, "operator": operator,
                   "batch_id": batch_id, "mode": mode,
                   "captured_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        (task_dir / "task.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return task_dir

    def upload(self, task_dir: Path) -> dict:
        payload = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        data = {
            "job_id": payload["job_id"],
            "station_id": payload["station_id"],
            "operator": payload.get("operator", ""),
            "mode": payload.get("mode", "geo"),
            "metadata": json.dumps({"captured_at": payload.get("captured_at", "")}, ensure_ascii=False),
        }
        if payload.get("batch_id"):
            data["batch_id"] = str(payload["batch_id"])
        if payload.get("template_id"):
            data["template_id"] = payload["template_id"]
        if payload.get("current_year"):
            data["current_year"] = str(payload["current_year"])
        if payload.get("threshold") is not None:
            data["threshold"] = str(payload["threshold"])
        with (task_dir / "front.jpg").open("rb") as front, (task_dir / "back.jpg").open("rb") as back:
            response = requests.post(
                f"{self.server_url}/api/remote/jobs", headers=self.headers, data=data,
                files={"front": ("front.jpg", front, "image/jpeg"),
                       "back": ("back.jpg", back, "image/jpeg")}, timeout=self.timeout,
            )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "服务器拒绝任务")
        target = self.cache_dir / "confirmed" / task_dir.name
        if target.exists():
            shutil.rmtree(target)
        task_dir.replace(target)
        return result

    def sync_pending(self) -> list[dict]:
        results = []
        for task_dir in sorted((self.cache_dir / "pending").iterdir()):
            if not task_dir.is_dir():
                continue
            try:
                results.append(self.upload(task_dir))
            except Exception as exc:
                results.append({"ok": False, "job_id": task_dir.name, "error": str(exc)})
        return results

    def wait(self, job_id: str) -> dict:
        with requests.get(
            f"{self.server_url}/api/remote/jobs/{job_id}/stream",
            headers=self.headers, timeout=(10, self.timeout), stream=True,
        ) as response:
            response.raise_for_status()
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line or not raw_line.startswith("data: "):
                    continue
                state = json.loads(raw_line[6:])
                print(f"[{state.get('status', '')}] {state.get('message', '')}")
                if state.get("status") in ("completed", "failed"):
                    return state
        raise RuntimeError("结果推送连接已断开")

    def get_job(self, job_id: str) -> dict:
        response = requests.get(
            f"{self.server_url}/api/remote/jobs/{job_id}",
            headers=self.headers,
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "任务查询失败")
        return result

    def wait_result(self, job_id: str, poll_seconds: float = 0.5) -> dict:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            state = self.get_job(job_id)["job"]
            if state.get("status") == "completed":
                return state.get("result") or {}
            if state.get("status") == "failed":
                raise RuntimeError(state.get("error") or "服务器识别失败")
            time.sleep(poll_seconds)
        raise TimeoutError("等待服务器识别结果超时")


def load_config(path: str) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    config["api_key"] = os.environ.get("QC_CLIENT_API_KEY", config.get("api_key", ""))
    required = [name for name in ("server_url", "api_key", "station_id") if not config.get(name)]
    if required:
        raise ValueError(f"客户端缺少配置：{', '.join(required)}")
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="云小圈质检远程相机客户端")
    parser.add_argument("--config", default="client/client_config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    capture = sub.add_parser("capture")
    capture.add_argument("--batch-id", type=int)
    capture.add_argument("--operator", default="")
    capture.add_argument("--wait", action="store_true")
    upload = sub.add_parser("upload-files")
    upload.add_argument("--front", required=True)
    upload.add_argument("--back", required=True)
    upload.add_argument("--batch-id", type=int)
    upload.add_argument("--operator", default="")
    sub.add_parser("sync")
    wait = sub.add_parser("wait")
    wait.add_argument("job_id")
    args = parser.parse_args()
    client = QCClient(load_config(args.config))
    if args.command == "health":
        print(json.dumps(client.health(), ensure_ascii=False, indent=2))
    elif args.command == "capture":
        task = client.capture(batch_id=args.batch_id, operator=args.operator)
        uploaded = client.upload(task)
        print(json.dumps(uploaded, ensure_ascii=False, indent=2))
        if args.wait:
            print(json.dumps(client.wait(uploaded["job"]["job_id"]), ensure_ascii=False, indent=2))
    elif args.command == "upload-files":
        task = client.queue_files(args.front, args.back, batch_id=args.batch_id, operator=args.operator)
        print(json.dumps(client.upload(task), ensure_ascii=False, indent=2))
    elif args.command == "sync":
        print(json.dumps(client.sync_pending(), ensure_ascii=False, indent=2))
    elif args.command == "wait":
        print(json.dumps(client.wait(args.job_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
