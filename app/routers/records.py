"""质检记录 + 操作人 接口（MySQL）。"""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Body, Form
from fastapi.responses import JSONResponse, StreamingResponse

from .. import services
from ..storage import db

router = APIRouter()

# 导出列：(表头, 记录字段)
_EXPORT_COLS = [
    ("时间", "created_at"), ("SN", "sn"), ("客户", "customer"), ("批次号", "batch_no"),
    ("品牌", "brand"), ("型号", "model"), ("容量", "capacity"), ("频率", "frequency"),
    ("规格", "spec"), ("厂商", "mfg"), ("成色", "cond"), ("托盘槽位", "slot_pos"),
    ("主控日期", "controller_date"), ("PCB日期", "pcb_date"),
    ("颗粒数", "storage_count"), ("颗粒日期明细", "storage_chips"),
    ("元器件", "comp_ok"), ("金手指", "gold_finger_ok"), ("芯片标记", "chip_mark_ok"),
    ("日期一致", "date_ok"),
    ("判定", "verdict"), ("不合格说明", "fail_desc"),
    ("复查", "review_status"), ("操作人", "operator"), ("备注", "remark"),
    ("检测批次ID", "inspection_id"), ("识别方式", "recognition_mode"),
    ("单次总耗时(秒)", "elapsed_sec"), ("分阶段耗时", "timing"),
    ("Token调用次数", "token_calls"), ("输入Token", "prompt_tokens"),
    ("输出Token", "completion_tokens"), ("总Token", "total_tokens"),
    ("二维码原文", "label_raw"), ("二维码来源", "label_src"),
]


def _ok_txt(v):
    """三态外观/日期布尔 → 中文（1 合格 / 0 不合格 / None 未检）。"""
    if v is None:
        return "未检"
    return "合格" if v else "不合格"


def _chips_txt(chips):
    """颗粒明细数组 → 可读文字：'正#1:2417 正#2:2418 反#1:2417 …'。"""
    if not chips:
        return ""
    side = {"front": "正", "back": "反", "pcb": "PCB"}
    parts = []
    for c in chips:
        s = side.get(c.get("side"), c.get("side") or "")
        idx = c.get("idx") if c.get("idx") is not None else "?"
        parts.append(f"{s}#{idx}:{c.get('yyyyww') or '—'}")
    return " ".join(parts)


_VERDICT_TXT = {"pass": "合格", "fail": "不合格", "unknown": "待定"}


# --------------------- 操作人（前台可选/可改/可增删）---------------------

@router.get("/api/operators")
def operators():
    try:
        return {"operators": db.list_operators()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"operators": [], "error": str(e)}, status_code=200)


@router.post("/api/operators")
def add_operator(name: str = Form(...)):
    try:
        db.add_operator(name)
        return {"ok": True, "operators": db.list_operators()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.delete("/api/operators/{name}")
def delete_operator(name: str):
    try:
        db.delete_operator(name)
        db.add_audit("", "delete_operator", name)
        return {"ok": True, "operators": db.list_operators()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# --------------------- 质检记录（绑定 SN + 操作人）---------------------

@router.get("/api/records")
def records(limit: int = 50):
    try:
        return {"records": db.list_records(limit)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"records": [], "error": str(e)}, status_code=200)


@router.post("/api/records")
def save_record(record: dict = Body(...)):
    try:
        # 追溯：把前端带来的原图/标注图（/uploads、/outputs）归档到永久区再入库
        services.archive_record_images(record)
        rid = db.save_record(record)
        return {"ok": True, "id": rid}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/records/by_sn/{sn}")
def records_by_sn(sn: str):
    """按 SN 追溯：该 SN 历次质检记录 + 同 SN 历史比对（日期变了→疑似被换芯片）。"""
    try:
        recs = db.list_records_by_sn(sn)
        return {"sn": sn, "records": recs, "diff": services.sn_history_diff(recs)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"sn": sn, "records": [], "error": str(e)}, status_code=200)


@router.get("/api/records/export")
def export_records(format: str = "xlsx", customer: str = "", batch_no: str = "",
                   verdict: str = "", date_from: str = "", date_to: str = "", limit: int = 2000):
    """按筛选条件导出质检记录（xlsx / csv）供对账/质检报告。"""
    try:
        rows = db.list_records_filtered(customer or None, batch_no or None, verdict or None,
                                        date_from or None, date_to or None, limit)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    headers = [h for h, _ in _EXPORT_COLS]
    keys = [k for _, k in _EXPORT_COLS]

    def cell(r, k):
        v = r.get(k)
        if k in ("comp_ok", "gold_finger_ok", "chip_mark_ok", "date_ok"):
            return _ok_txt(v)
        if k == "storage_chips":
            return _chips_txt(v)
        if k == "verdict":
            return _VERDICT_TXT.get(v, v or "")
        if k == "timing":
            return json.dumps(r.get("timing") or {}, ensure_ascii=False)
        if k in ("token_calls", "prompt_tokens", "completion_tokens", "total_tokens"):
            field = "calls" if k == "token_calls" else k
            return (r.get("token_usage") or {}).get(field, 0)
        if k in ("label_raw", "label_src"):
            field = "raw" if k == "label_raw" else "src"
            return (r.get("label_data") or {}).get(field, "")
        return "" if v is None else v

    if format == "csv":
        buf = io.StringIO()
        buf.write("﻿")                       # BOM，Excel 打开中文不乱码
        w = csv.writer(buf)
        w.writerow(headers)
        for r in rows:
            w.writerow([cell(r, k) for k in keys])
        data = buf.getvalue().encode("utf-8")
        return StreamingResponse(io.BytesIO(data), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="records.csv"'})

    # xlsx
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "质检记录"
    ws.append(headers)
    for r in rows:
        ws.append([cell(r, k) for k in keys])
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return StreamingResponse(
        out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="records.xlsx"'})


@router.post("/api/records/{rid}/review")
def set_review(rid: int, status: str = Form(...), operator: str = Form("")):
    try:
        ok = db.update_review(rid, status)
        if ok:
            db.add_audit(operator, "review", str(rid), status)
        return {"ok": ok} if ok else JSONResponse(
            {"ok": False, "error": "无效状态或记录不存在"}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/audit")
def audit(limit: int = 100):
    """审计日志（复查变更、删除等敏感操作）。"""
    try:
        return {"audit": db.list_audit(limit)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"audit": [], "error": str(e)}, status_code=200)
