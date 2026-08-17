from __future__ import annotations

import json
import os
import queue

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from ..remote_jobs import RemoteImageQualityError, manager


router = APIRouter(prefix="/api/remote")


def _authorize(client_key: str | None) -> None:
    expected = (os.environ.get("CLIENT_API_KEY") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="服务端未配置 CLIENT_API_KEY")
    if client_key != expected:
        raise HTTPException(status_code=401, detail="客户端认证失败")


@router.get("/health")
def remote_health(x_client_key: str | None = Header(None)):
    _authorize(x_client_key)
    return {"ok": True, "service": "remote-inspection", "gpu_workers": 1}


@router.post("/jobs")
def create_job(
    front: UploadFile = File(...),
    back: UploadFile = File(...),
    job_id: str = Form(...),
    station_id: str = Form(...),
    operator: str = Form(""),
    batch_id: int | None = Form(None),
    mode: str = Form("geo"),
    template_id: str | None = Form(None),
    current_year: int | None = Form(None),
    threshold: float | None = Form(None),
    metadata: str = Form("{}"),
    x_client_key: str | None = Header(None),
):
    _authorize(x_client_key)
    try:
        metadata_value = json.loads(metadata or "{}")
        if not isinstance(metadata_value, dict):
            raise ValueError("metadata 必须是 JSON 对象")
        state, created = manager.submit(
            job_id=job_id, front=front, back=back, station_id=station_id,
            operator=operator, batch_id=batch_id, mode=mode,
            template_id=template_id, current_year=current_year, threshold=threshold,
            metadata=metadata_value,
        )
    except RemoteImageQualityError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "created": created, "job": state,
            "queue_position": manager.queue_position(state["job_id"])}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, x_client_key: str | None = Header(None)):
    _authorize(x_client_key)
    state = manager.get(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True, "job": state, "queue_position": manager.queue_position(job_id)}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, x_client_key: str | None = Header(None)):
    _authorize(x_client_key)
    try:
        state = manager.retry(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在") from None
    return {"ok": True, "job": state, "queue_position": manager.queue_position(job_id)}


@router.get("/jobs/{job_id}/stream")
def stream_job(job_id: str, x_client_key: str | None = Header(None)):
    _authorize(x_client_key)
    state = manager.get(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="任务不存在")

    def events():
        channel = manager.subscribe(job_id)
        yield f"data: {json.dumps({'type': 'status', **state}, ensure_ascii=False)}\n\n"
        try:
            while True:
                try:
                    event = channel.get(timeout=15)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("status") in ("completed", "failed"):
                        break
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            manager.unsubscribe(job_id, channel)

    return StreamingResponse(events(), media_type="text/event-stream")
