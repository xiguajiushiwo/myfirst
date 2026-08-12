"""检测进度 SSE；自动目录监听入口保留兼容响应，但不再启动检测。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse, StreamingResponse

from ..pipeline.runner import runner as pipeline_runner

router = APIRouter()


@router.post("/api/pipeline/start")
def pipeline_start(
    operator: str = Form(""),
    mode: str = Form("geo"),
    template_id: str | None = Form(None),
    current_year: int | None = Form(None),
    threshold: float | None = Form(None),
    interval: float = Form(1.0),
    batch_id: int | None = Form(None),
):
    """自动目录监听已停用；检测只允许由操作员手动拍照触发。"""
    pipeline_runner.stop()
    return JSONResponse({"ok": False, "error": "自动检测已停用，请手动点击拍照检测"}, status_code=200)


@router.post("/api/pipeline/stop")
def pipeline_stop():
    pipeline_runner.stop()
    return {"ok": True}


@router.get("/api/pipeline/status")
def pipeline_status():
    return pipeline_runner.status()


@router.get("/api/pipeline/stream")
def pipeline_stream():
    """SSE 实时推送每根处理结果，供看板刷新。"""
    def gen():
        q = pipeline_runner.subscribe()
        # 先推一次当前状态
        yield f"data: {json.dumps({'type':'status', **pipeline_runner.status()}, ensure_ascii=False)}\n\n"
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except Exception:
                    yield ": keep-alive\n\n"       # 心跳，防连接超时
        finally:
            pipeline_runner.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream")
