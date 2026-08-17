from __future__ import annotations

import argparse
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

from app.cameras import cam_supervisor
from app.cameras import hik_camera as hik
from client.qc_client import QCClient, load_config


MJPEG = "multipart/x-mixed-replace; boundary=frame"


def create_app(config_path: str = "client/client_config.json") -> FastAPI:
    client = QCClient(load_config(config_path))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        cam_supervisor.start()
        deadline = time.time() + 30
        while time.time() < deadline and not hik.status().get("sdk"):
            time.sleep(0.5)
        yield
        cam_supervisor.stop()

    app = FastAPI(title="云小圈客户机采集代理", version="1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def allow_local_browser_access(request, call_next):
        response = await call_next(request)
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index():
        camera = hik.status()
        try:
            server = client.health()
        except Exception as exc:  # noqa: BLE001
            server = {"ok": False, "error": str(exc)}
        camera_ok = bool(camera.get("sdk"))
        server_ok = bool(server.get("ok"))
        camera_text = "正常" if camera_ok else "异常"
        server_text = "正常" if server_ok else "异常"
        server_error = "" if server_ok else f"<p>服务器连接错误：{server.get('error', '')}</p>"
        return f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>云小圈客户机采集代理</title>
<style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:32px;line-height:1.6;color:#172033;background:#f6f8fb}}
.panel{{max-width:720px;background:#fff;border:1px solid #dbe2ea;border-radius:8px;padding:24px;box-shadow:0 8px 24px rgba(20,35,55,.08)}}
h1{{font-size:22px;margin:0 0 16px}}
.row{{display:flex;gap:12px;align-items:center;margin:10px 0}}
.pill{{display:inline-flex;min-width:56px;justify-content:center;border-radius:999px;padding:2px 10px;font-size:13px;font-weight:700}}
.ok{{background:#e7f7ec;color:#12652b}}.bad{{background:#feecea;color:#a7261d}}
code{{background:#f1f4f8;padding:2px 6px;border-radius:5px}}
a{{color:#155bd5}}
</style>
<div class="panel">
  <h1>云小圈客户机采集代理</h1>
  <div class="row"><span>工位：</span><code>{client.station_id}</code></div>
  <div class="row"><span>相机 SDK：</span><span class="pill {'ok' if camera_ok else 'bad'}">{camera_text}</span></div>
  <div class="row"><span>服务端连接：</span><span class="pill {'ok' if server_ok else 'bad'}">{server_text}</span></div>
  {server_error}
  <p>这个端口是本机相机采集代理，不是主页面。请在接相机的这台电脑浏览器打开服务端页面：</p>
  <p><code>{client.server_url}/camera</code></p>
  <p>诊断入口：<a href="/health">/health</a>，正面预览：<a href="/camera/preview?side=front">/camera/preview?side=front</a>，反面预览：<a href="/camera/preview?side=back">/camera/preview?side=back</a></p>
</div>
</html>"""

    @app.get("/health")
    def health():
        camera = hik.status()
        try:
            server = client.health()
        except Exception as exc:  # noqa: BLE001
            server = {"ok": False, "error": str(exc)}
        return {"ok": bool(camera.get("sdk")), "station_id": client.station_id,
                "camera": camera, "server": server}

    @app.get("/camera/status")
    def camera_status():
        return hik.status()

    @app.get("/camera/preview")
    def camera_preview(side: str = "front", quality: int = 78, max_fps: int = 12):
        quality = max(40, min(95, int(quality)))
        max_fps = max(1, min(30, int(max_fps)))
        return StreamingResponse(
            hik.mjpeg(side, quality=quality, max_fps=max_fps),
            media_type=MJPEG,
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/camera/exposure")
    def get_exposure(side: str = "front"):
        try:
            return {"ok": True, **hik.get_exposure(side)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.post("/camera/exposure")
    def set_exposure(side: str = Form("front"), exposure_us: float = Form(...)):
        try:
            return {"ok": True, **hik.set_exposure(exposure_us, side)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.get("/camera/gain")
    def get_gain(side: str = "front"):
        try:
            return {"ok": True, **hik.get_gain(side)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.post("/camera/gain")
    def set_gain(side: str = Form("front"), gain_db: float = Form(...)):
        try:
            return {"ok": True, **hik.set_gain(gain_db, side)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.get("/camera/orient")
    def get_orient():
        return {"ok": True, "orient": hik.get_orient()}

    @app.post("/camera/orient")
    def set_orient(side: str = Form(...), mode: str = Form(...)):
        try:
            return {"ok": True, "side": side, "mode": hik.set_orient(side, mode)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.post("/capture-and-recognize")
    def capture_and_recognize(
        batch_id: int | None = Form(None),
        operator: str = Form(""),
        mode: str = Form("geo"),
        template_id: str | None = Form(None),
        current_year: int | None = Form(None),
        threshold: float | None = Form(None),
    ):
        try:
            task_dir = client.capture(
                batch_id=batch_id, operator=operator, mode=mode,
                template_id=template_id, current_year=current_year, threshold=threshold,
            )
            submitted = client.upload(task_dir)
            job_id = submitted["job"]["job_id"]
            result = client.wait_result(job_id)
            if isinstance(result, dict):
                result.setdefault("remote_job_id", job_id)
                result.setdefault("station_id", client.station_id)
            return result
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="云小圈客户机本地相机采集代理")
    parser.add_argument("--config", default="client/client_config.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8812)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(create_app(args.config), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
