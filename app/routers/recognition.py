"""识别相关接口：型号模板、读标签、读取文件夹、识别、外观质检。"""
from __future__ import annotations

import os
import shutil
import time
import uuid

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

from .. import services
from ..core import TEST_DIR, UPLOAD_DIR
from ..recognition import template_store
from ..storage import db
from ..inspection.quality_inspect import inspect_module, read_label_vl

router = APIRouter()


# --------------------- 型号模板 ---------------------

@router.get("/api/templates")
def templates():
    """型号模板列表（前端下拉 + 模板管理）。"""
    return {"templates": template_store.list_templates(),
            "default": template_store.default_template_id()}


@router.delete("/api/templates/{template_id}")
def delete_template(template_id: str):
    ok = template_store.delete_template(template_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "模板不存在"}, status_code=404)
    try:
        db.add_audit("", "delete_template", template_id)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "deleted": template_id}


# --------------------- 读标签（品牌/型号/频率/SN，大模型）---------------------

@router.post("/api/read_label")
async def read_label(front: UploadFile = File(...)):
    """读取正面标签，返回 {brand, model, frequency, sn}，供前端自动填充（可人工改）。"""
    uid = uuid.uuid4().hex[:12]
    in_path = await services.save_upload(front, uid, "label")
    try:
        return {"ok": True, **read_label_vl(in_path)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


# --------------------- 读取服务器文件夹照片（测试用）---------------------

@router.get("/api/folder/list")
def folder_list():
    """列出 test_photos/ 下可识别的图片组（每个子文件夹=一根内存条；根目录散图算一组）。"""
    sets = []
    root = services.resolve_set(TEST_DIR)
    if root:
        sets.append({"name": "__root__", "label": "(根目录散图)",
                     "slots": {k: os.path.basename(v) for k, v in root.items()}})
    for name in sorted(os.listdir(TEST_DIR)):
        d = os.path.join(TEST_DIR, name)
        if os.path.isdir(d):
            s = services.resolve_set(d)
            if s:
                sets.append({"name": name, "label": name,
                             "slots": {k: os.path.basename(v) for k, v in s.items()}})
    return {"dir": TEST_DIR, "sets": sets}


@router.post("/api/folder/recognize")
def folder_recognize(
    name: str = Form(""),
    current_year: int | None = Form(None),
    template_id: str | None = Form(None),
    mode: str | None = Form("rules"),
    threshold: float | None = Form(None),
    vl_check: bool = Form(False),
):
    """识别 test_photos/ 下某组照片（name=子文件夹名，或 __root__ 表示根目录散图）。"""
    folder = TEST_DIR if (not name or name == "__root__") else os.path.join(TEST_DIR, name)
    folder = os.path.abspath(folder)
    if not folder.startswith(os.path.abspath(TEST_DIR)) or not os.path.isdir(folder):
        return JSONResponse({"ok": False, "error": "无效文件夹"}, status_code=400)
    slots = services.resolve_set(folder)
    if not slots:
        return JSONResponse({"ok": False, "error": "该文件夹下没有可识别的图片"}, status_code=200)

    uid = uuid.uuid4().hex[:12]
    paths = {}
    for slot, src in slots.items():
        dst = os.path.join(UPLOAD_DIR, f"{uid}_{slot}{os.path.splitext(src)[1].lower()}")
        shutil.copyfile(src, dst)
        paths[slot] = dst
    try:
        rec, insp, label = services.analyze_all(paths, uid, mode, template_id, current_year, threshold, vl_check)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "recognize": rec, "inspect": insp, "label": label})


# --------------------- 识别 / 外观质检（手动上传）---------------------

@router.post("/api/recognize")
async def recognize(
    front: UploadFile | None = File(None),
    back: UploadFile | None = File(None),
    pcb: UploadFile | None = File(None),
    controller: UploadFile | None = File(None),
    current_year: int | None = Form(None),
    template_id: str | None = Form(None),
    mode: str | None = Form("rules"),
    threshold: float | None = Form(None),
    vl_check: bool = Form(False),
):
    """识别内存条日期码（手动上传，至少一张）。"""
    t0 = time.perf_counter()
    uploads = {"front": front, "back": back, "pcb": pcb, "controller": controller}
    if all(uf is None for uf in uploads.values()):
        return JSONResponse({"ok": False, "error": "请至少上传一张图片"}, status_code=400)
    uid = uuid.uuid4().hex[:12]
    paths = {}
    for slot, uf in uploads.items():
        if uf is not None:
            paths[slot] = await services.save_upload(uf, uid, slot)
    try:
        out = services.run_recognize(paths, uid, mode, template_id, current_year, threshold, vl_check)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    out["elapsed_sec"] = round(time.perf_counter() - t0, 2)
    return JSONResponse(out)


@router.post("/api/inspect")
async def inspect(
    front: UploadFile | None = File(None),
    back: UploadFile | None = File(None),
):
    """外观质检：把正/背面照片交给 Qwen-VL 大模型，检查元器件损坏/发黑、
    金手指、以及存储芯片二维码标记是否有线条；任一异常亮红灯并给出原因。
    """
    t0 = time.perf_counter()
    if front is None and back is None:
        return JSONResponse({"ok": False, "error": "请至少上传一张图片"}, status_code=400)

    uid = uuid.uuid4().hex[:12]
    paths = {}
    for slot, uf in (("front", front), ("back", back)):
        if uf is not None:
            paths[slot] = await services.save_upload(uf, uid, f"inspect_{slot}")

    # elapsed_sec=整体（含图片编码+调用）；model_sec=仅大模型调用（inspect_module 内计）
    result = inspect_module(paths.get("front"), paths.get("back"))
    result["elapsed_sec"] = round(time.perf_counter() - t0, 2)
    result["images"] = {
        slot: f"/uploads/{os.path.basename(p)}" for slot, p in paths.items()
    }
    status = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status)
