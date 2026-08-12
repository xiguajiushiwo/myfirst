"""相机接口：服务器端 UVC 双相机 + 海康工业相机（MVS SDK）。"""
from __future__ import annotations

import os
import threading
import time
import uuid

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse, StreamingResponse

from .. import services
from ..core import UPLOAD_DIR
from ..cameras import camera
from ..cameras import hik_camera as hik
from ..storage import db

router = APIRouter()
_MANUAL_INSPECTION_LOCK = threading.Lock()

_MJPEG = "multipart/x-mixed-replace; boundary=frame"


def _next_seq_dir() -> tuple[str, str]:
    """在 uploads/ 下新建一个按顺序编号的子文件夹（0001、0002…），代表拍摄先后。

    每次双相机抓拍的 front.jpg + back.jpg 都放进这样一个子文件夹里，
    uploads/ 根目录保持整洁（只含有序子文件夹，不再堆散图）。
    返回 (绝对路径, 序号名)。
    """
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    mx = 0
    for n in os.listdir(UPLOAD_DIR):
        if os.path.isdir(os.path.join(UPLOAD_DIR, n)) and n.isdigit():
            mx = max(mx, int(n))
    seq = f"{mx + 1:04d}"
    d = os.path.join(UPLOAD_DIR, seq)
    os.makedirs(d, exist_ok=True)
    return d, seq


# --------------------- 服务器端双相机采集（UVC，手动拍照）---------------------

@router.get("/api/camera/status")
def camera_status():
    """各路相机是否可用（相机插在服务器上，任意电脑访问本接口）。"""
    try:
        return camera.status()
    except Exception as e:  # noqa: BLE001
        return {"front": False, "back": False, "error": str(e)}


@router.get("/api/camera/preview/{side}")
def camera_preview(side: str):
    """某一路相机的 MJPEG 实时预览流（任意浏览器 <img> 直接看）。"""
    return StreamingResponse(camera.mjpeg(side), media_type=_MJPEG)


@router.post("/api/camera/capture")
def camera_capture(
    current_year: int | None = Form(None),
    template_id: str | None = Form(None),
    mode: str | None = Form("geo"),
    threshold: float | None = Form(None),
):
    """手动拍照：抓取正/背面两台相机当前帧 → 识别 + 外观质检 + 读标签。"""
    uid = uuid.uuid4().hex[:12]
    paths = {}
    for side in ("front", "back"):
        p = os.path.join(UPLOAD_DIR, f"{uid}_{side}.jpg")
        if camera.snapshot(side, p):
            paths[side] = p
    if not paths:
        return JSONResponse({"ok": False, "error": "相机不可用或未抓到画面，请检查相机连接/序号(.env)"},
                            status_code=200)
    try:
        rec, insp, label = services.analyze_all(paths, uid, mode, template_id, current_year, threshold)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "recognize": rec, "inspect": insp, "label": label})


# --------------------- 海康工业相机（MVS SDK）---------------------

def _nic_link() -> dict:
    """接相机的网卡链路速度（link-local 且 up 的那块）。<1Gbps 会导致相机取帧超时/掉线。"""
    try:
        import psutil
        stats, addrs = psutil.net_if_stats(), psutil.net_if_addrs()
        best = None
        for name, al in addrs.items():
            if any(str(getattr(a, "address", "")).startswith("169.254") for a in al):
                sp = stats.get(name)
                if sp and sp.isup and sp.speed > 0:
                    if best is None or sp.speed > best["speed_mbps"]:
                        best = {"name": name, "speed_mbps": int(sp.speed)}
        if best:
            best["warn"] = best["speed_mbps"] < 1000
            return best
    except Exception:  # noqa: BLE001
        pass
    return {}


@router.get("/api/hik/status")
def hik_status():
    """海康相机可用性 + 已连接设备（型号/SN/IP）+ 网卡链路速度。不抛错。"""
    try:
        st = hik.status()
    except Exception as e:  # noqa: BLE001
        st = {"sdk": False, "error": str(e), "devices": []}
    st["net"] = _nic_link()                 # 前端据此在 <1Gbps 时告警
    return st


@router.post("/api/hik/release")
def hik_release():
    """释放所有相机（停自动+关流+关句柄），让海康 MVS 客户端能独占接管（调参用）。"""
    try:
        from ..pipeline.motion_trigger import motion_trigger
        motion_trigger.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        hik.close()
        hik.pause()                                # 暂停自愈：别让看门狗/预览流把相机抢回来(留给 MVS)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
    return {"ok": True}


@router.get("/api/hik/preview")
def hik_preview(side: str = "front"):
    """海康相机实时预览 MJPEG 流。`side=front`(上/正面) / `back`(下/反面)。"""
    role = side if side in ("front", "back") else "front"
    hik.resume()                                   # 要用相机→解除释放暂停(允许自愈)
    return StreamingResponse(hik.mjpeg(role=role), media_type=_MJPEG)


@router.get("/api/hik/exposure")
def hik_get_exposure(side: str = "front"):
    """读某台(front/back)当前曝光时间(微秒)及可调范围。"""
    role = side if side in ("front", "back") else "front"
    try:
        return {"ok": True, "side": role, **hik.get_exposure(role=role)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@router.post("/api/hik/exposure")
def hik_set_exposure(exposure_us: float = Form(...), side: str = Form("front")):
    """设置某台(front/back)曝光时间(微秒)。返回实际生效的当前值与范围。"""
    role = side if side in ("front", "back") else "front"
    try:
        return {"ok": True, "side": role, **hik.set_exposure(exposure_us, role=role)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@router.get("/api/hik/gain")
def hik_get_gain(side: str = "front"):
    """读某台(front/back)当前增益(dB)及范围。"""
    role = side if side in ("front", "back") else "front"
    try:
        return {"ok": True, "side": role, **hik.get_gain(role=role)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@router.post("/api/hik/gain")
def hik_set_gain(gain_db: float = Form(...), side: str = Form("front")):
    """设置某台(front/back)增益(dB)。上下两台光照不同，各调各的。"""
    role = side if side in ("front", "back") else "front"
    try:
        return {"ok": True, "side": role, **hik.set_gain(gain_db, role=role)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@router.get("/api/hik/orient")
def hik_get_orient():
    """当前上/下相机的方向校正模式（反面翻正，让正/反同一根对齐）。"""
    return {"ok": True, "orient": hik.get_orient()}


@router.post("/api/hik/orient")
def hik_set_orient(side: str = Form(...), mode: str = Form(...)):
    """设某台方向校正：none(不翻)/fliph(左右镜像)/flipv(上下)/rot180。即时对预览+抓拍生效。"""
    try:
        m = hik.set_orient(side, mode)
        return {"ok": True, "side": side, "mode": m}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


def _safe_uid(uid: str) -> str:
    """只保留字母数字，防路径穿越；空则给随机。"""
    u = "".join(c for c in (uid or "") if c.isalnum())[:32]
    return u or uuid.uuid4().hex[:12]


@router.post("/api/hik/capture")
def hik_capture(side: str = Form("front"), uid: str = Form("")):
    """海康相机抓拍一帧存到 uploads。`side=front`(上相机) / `back`(下相机)；正/反面用同一 uid。"""
    side = side if side in ("front", "back") else "front"
    uid = _safe_uid(uid)
    out = os.path.join(UPLOAD_DIR, f"{uid}_{side}.jpg")
    try:
        hik.snapshot(out, role=side)
        return {"ok": True, "side": side, "uid": uid, "path": out,
                "image_url": f"/uploads/{os.path.basename(out)}"}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@router.post("/api/hik/capture_both")
def hik_capture_both(uid: str = Form("")):
    """**双相机同时抓拍**：上相机拍正面 + 下相机拍反面，存同一 uid 的 front/back，供随后一起识别。"""
    d, seq = _next_seq_dir()
    fp = os.path.join(d, "front.jpg")
    bp = os.path.join(d, "back.jpg")
    try:
        hik.capture_both(fp, bp)
        return {"ok": True, "uid": seq, "seq": seq,
                "front_url": f"/uploads/{seq}/front.jpg",
                "back_url": f"/uploads/{seq}/back.jpg"}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@router.post("/api/hik/single/capture_front")
def hik_single_capture_front(backend: str = Form("hik")):
    """单相机第一步：拍正面并保留本次检测目录，等待操作员翻面。"""
    with _MANUAL_INSPECTION_LOCK:
        d, seq = _next_seq_dir()
        path = os.path.join(d, "front.jpg")
        started = time.perf_counter()
        try:
            if backend == "uvc":
                if not camera.snapshot("front", path):
                    raise RuntimeError("USB 相机未抓到画面")
            else:
                hik.snapshot(path, role="front")
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
        capture_sec = round(time.perf_counter() - started, 3)
    return {"ok": True, "uid": seq, "capture_sec": capture_sec,
            "front_url": f"/uploads/{seq}/front.jpg",
            "message": "正面已拍摄，请将整盘翻到反面后再次点击"}


@router.post("/api/hik/single/capture_back_and_save")
def hik_single_capture_back_and_save(
    uid: str = Form(...),
    front_capture_sec: float = Form(0),
    backend: str = Form("hik"),
    operator: str = Form(""),
    mode: str = Form("geo"),
    template_id: str | None = Form(None),
    current_year: int | None = Form(None),
    threshold: float | None = Form(None),
    batch_id: int | None = Form(None),
):
    """单相机第二步：同一相机拍反面，然后识别并入库。

    mode: "geo"（默认，逐槽整合颗粒/PCB/主控）/ "rules"（旧整图规则识别）。
    """
    from ..pipeline.feeder import Job

    seq = _safe_uid(uid)
    d = os.path.join(UPLOAD_DIR, seq)
    front_path = os.path.join(d, "front.jpg")
    back_path = os.path.join(d, "back.jpg")
    if not seq.isdigit() or not os.path.isfile(front_path):
        return JSONResponse({"ok": False, "error": "正面照片不存在，请重新从第一步拍摄"}, status_code=200)

    with _MANUAL_INSPECTION_LOCK:
        finish_started = time.perf_counter()
        capture_started = time.perf_counter()
        try:
            if backend == "uvc":
                if not camera.snapshot("front", back_path):
                    raise RuntimeError("USB 相机未抓到反面画面")
            else:
                hik.snapshot(back_path, role="front")
            back_capture_sec = round(time.perf_counter() - capture_started, 3)
            result = services.analyze_and_save(
                Job(pos_id=seq, paths={"front": front_path, "back": back_path}),
                operator=operator, mode=mode, template_id=template_id,
                current_year=current_year, threshold=threshold, batch_id=batch_id, save=True)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

        timing = dict(result.get("timing") or {})
        timing["analysis_total"] = timing.get("total", result.get("elapsed_sec", 0))
        timing["capture_front"] = round(max(0.0, float(front_capture_sec or 0)), 3)
        timing["capture_back"] = back_capture_sec
        timing["capture"] = round(timing["capture_front"] + timing["capture_back"], 3)
        timing["total"] = round(timing["capture_front"] + time.perf_counter() - finish_started, 3)
        result.update({"timing": timing, "elapsed_sec": timing["total"], "camera_mode": "single"})
        record_ids = [s.get("record_id") for s in result.get("sticks") or [] if s.get("record_id")]
        if result.get("record_id"):
            record_ids.append(result["record_id"])
        if record_ids:
            db.update_record_runtime(record_ids, timing, result.get("token_usage") or {}, timing["total"])
    return JSONResponse(result)


@router.post("/api/hik/single/capture_and_save")
def hik_single_capture_and_save(
    backend: str = Form("hik"),
    operator: str = Form(""),
    mode: str = Form("geo"),
    template_id: str | None = Form(None),
    current_year: int | None = Form(None),
    threshold: float | None = Form(None),
    batch_id: int | None = Form(None),
    save: bool = Form(True),
):
    """**单面一步到位**：只拍一张（上相机/唯一相机）→ 立刻识别入库，不要求翻面。

    与 `single/capture_front` + `single/capture_back_and_save` 那条两步流程**并存**，
    两步流程一行未动 —— 需要正反都看的产线仍可用它。

    只有一张图时下游天然降级、不需要额外分支：
      `_pre_crops` 逐面取图，back 没路径就不参与占位检测（槽位仍由正面定出）；
      当前仅运行本地日期 OCR 与正面二维码解码。
    **代价要说清**：反面那 80 颗颗粒和 PCB/PMIC 这次完全没看 —— 按铁律这不是
    "合格"，是"没检查"。所以返回里带 `single_side` 与 `single_side_warn`，
    前端必须显示，避免把"只查了一面"当成整根都合格。
    """
    from ..pipeline.feeder import Job
    from ..pipeline.runner import runner as _runner

    def _stage(name, text, **kw):
        try:
            _runner.push_stage(name, text, **kw)
        except Exception:  # noqa: BLE001
            pass

    # 缺省及旧前端传来的 template 都统一走整合识别；仅显式 rules 才走旧模式。
    mode = "rules" if (mode or "").lower() == "rules" else "geo"
    total_started = time.perf_counter()
    try:
        with _MANUAL_INSPECTION_LOCK:
            d, seq = _next_seq_dir()
            fp = os.path.join(d, "front.jpg")
            _stage("capture", "拍摄单面…")
            t_cap = time.perf_counter()
            if backend == "uvc":
                if not camera.snapshot("front", fp):
                    raise RuntimeError("USB 相机未抓到画面")
            else:
                hik.snapshot(fp, role="front")
            capture_sec = round(time.perf_counter() - t_cap, 2)
            _stage("recognize", f"已拍照({capture_sec}s)，PaddleOCR 逐颗识别日期…", capture=capture_sec)
            result = services.analyze_and_save(
                Job(pos_id=seq, paths={"front": fp}),
                operator=operator, mode=mode, template_id=template_id,
                current_year=current_year, threshold=threshold, batch_id=batch_id,
                save=save, on_stage=_stage)
    except ValueError as e:                          # 模板缺失等
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

    if isinstance(result, dict):
        tm = dict(result.get("timing") or {})
        tm["analysis_total"] = tm.get("total", result.get("elapsed_sec", 0))
        tm["capture"] = capture_sec
        tm["total"] = round(time.perf_counter() - total_started, 3)
        result["timing"] = tm
        result["elapsed_sec"] = tm["total"]
        result["camera_mode"] = "single"
        result["single_side"] = True
        result["single_side_warn"] = "本次只拍正面：反面存储颗粒与 PCB 未检查；正面被标签遮挡的主控不做 OCR，需人工确认"
        record_ids = [s.get("record_id") for s in result.get("sticks") or [] if s.get("record_id")]
        if result.get("record_id"):
            record_ids.append(result["record_id"])
        if save and record_ids:
            db.update_record_runtime(record_ids, tm, result.get("token_usage") or {}, tm["total"])
    return JSONResponse(result)


def _capture_and_analyze(operator="", mode="geo", template_id=None,
                         current_year=None, threshold=None, batch_id=None, save=True) -> dict:
    """双相机同时拍正反 → `analyze_and_save`（拆 N 条，逐根读二维码）。

    save=False：只识别不入库（复核用），逐根拆分/逐根读 SN 逻辑一致。
    """
    from ..pipeline.feeder import Job
    from ..pipeline.runner import runner as _runner

    def _stage(name, text, **kw):
        """链路进度实时推给前端(SSE)：整条 5~15s，不推的话操作员看不出是否在动。"""
        try:
            _runner.push_stage(name, text, **kw)
        except Exception:  # noqa: BLE001
            pass

    total_started = time.perf_counter()
    # 缺省及旧前端传来的 template 都统一走整合识别；仅显式 rules 才走旧模式。
    # 收窄在这里做一次，`analyze_and_save` 里还会再收窄一次 —— 两处都留着，
    # 因为那个函数也被别的调用方直接用。
    mode = "rules" if (mode or "").lower() == "rules" else "geo"
    d, seq = _next_seq_dir()
    fp = os.path.join(d, "front.jpg")
    bp = os.path.join(d, "back.jpg")
    _stage("capture", "双相机同时拍摄正反面…")
    t_cap = time.perf_counter()
    hik.capture_both(fp, bp)               # 上下相机同时抓（失败会抛错）
    capture_sec = round(time.perf_counter() - t_cap, 2)
    _stage("recognize", f"已拍照({capture_sec}s)，PaddleOCR 逐颗识别日期…", capture=capture_sec)
    job = Job(pos_id=seq, paths={"front": fp, "back": bp})
    result = services.analyze_and_save(
        job, operator=operator, mode=mode, template_id=template_id,
        current_year=current_year, threshold=threshold, batch_id=batch_id, save=save,
        on_stage=_stage)
    # 拍照和分析的总耗时取整条请求的实际墙钟，不把并行分支相加。
    if isinstance(result, dict):
        tm = dict(result.get("timing") or {})
        tm["analysis_total"] = tm.get("total", result.get("elapsed_sec", 0))
        tm["capture"] = capture_sec
        tm["total"] = round(time.perf_counter() - total_started, 3)
        result["timing"] = tm
        result["elapsed_sec"] = tm["total"]
        record_ids = [s.get("record_id") for s in result.get("sticks") or [] if s.get("record_id")]
        if result.get("record_id"):
            record_ids.append(result["record_id"])
        if save and record_ids:
            db.update_record_runtime(record_ids, tm, result.get("token_usage") or {}, tm["total"])
    return result


@router.post("/api/hik/capture_and_save")
def hik_capture_and_save(
    operator: str = Form(""),
    mode: str = Form("geo"),
    template_id: str | None = Form(None),
    current_year: int | None = Form(None),
    threshold: float | None = Form(None),
    batch_id: int | None = Form(None),
    uid: str = Form(""),
):
    """**一键：双相机同时拍正反 → 识别 → 拆 N 条入库**（放盘→双拍→识别 一步到位）。

    mode: "geo"（默认，逐槽整合颗粒/PCB/主控）/ "rules"（旧整图规则识别）。
    """
    try:
        with _MANUAL_INSPECTION_LOCK:
            result = _capture_and_analyze(operator, mode, template_id, current_year, threshold, batch_id)
    except ValueError as e:                # 模板缺失等
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
    return JSONResponse(result)


# --------------------- 自动触发已停用，仅保留停止/状态兼容接口 ---------------------

@router.post("/api/hik/auto/start")
def hik_auto_start(
    operator: str = Form(""),
    mode: str = Form("geo"),
    template_id: str | None = Form(None),
    current_year: int | None = Form(None),
    threshold: float | None = Form(None),
    batch_id: int | None = Form(None),
):
    """自动检测已停用；质检必须由操作员手动点击拍照。"""
    from ..pipeline.motion_trigger import motion_trigger
    motion_trigger.stop()
    return JSONResponse({"ok": False, "error": "自动检测已停用，请手动点击“拍照并检测”"}, status_code=200)


@router.post("/api/hik/auto/stop")
def hik_auto_stop():
    from ..pipeline.motion_trigger import motion_trigger
    motion_trigger.stop()
    return {"ok": True}


@router.get("/api/hik/auto/status")
def hik_auto_status():
    from ..pipeline.motion_trigger import motion_trigger
    return motion_trigger.status()


@router.post("/api/hik/recognize")
def hik_recognize(
    uid: str = Form(...),
    mode: str | None = Form("geo"),
    template_id: str | None = Form(None),
    current_year: int | None = Form(None),
    threshold: float | None = Form(None),
    vl_check: bool = Form(False),
):
    """识别刚用海康拍的正/反面（按 uid 从 uploads 取 {uid}_front.jpg / _back.jpg）。

    返回 {ok, recognize, inspect, label}，与文件夹识别同结构，前端复用 renderCombined。
    """
    uid = _safe_uid(uid)
    paths = {}
    for side in ("front", "back"):
        p = os.path.join(UPLOAD_DIR, f"{uid}_{side}.jpg")
        if os.path.isfile(p):
            paths[side] = p
    if not paths:
        return JSONResponse({"ok": False, "error": "没有已拍的照片，请先拍正面/反面"}, status_code=200)
    try:
        rec, insp, label = services.analyze_all(paths, uid, mode, template_id, current_year, threshold, vl_check)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "recognize": rec, "inspect": insp, "label": label})


@router.post("/api/hik/capture_both_recognize")
def hik_capture_both_recognize(
    operator: str = Form(""),
    mode: str = Form("geo"),
    template_id: str | None = Form(None),
    current_year: int | None = Form(None),
    threshold: float | None = Form(None),
):
    """**手动一键（不入库）**：双相机同时拍正反（整盘 N 根）→ **逐根识别 + 逐根读二维码**，
    返回多根结果供复核，**不写库**。

    返回结构与 /api/hik/capture_and_save 一致（多根 `sticks` 各带自己的 SN），前端复用
    renderCaptureSaveResult 显示全部 N 个二维码/SN。确认无误后再走入库。
    托盘模板仅提供四槽几何位置，供逐根裁图和二维码解码。
    mode: "geo"（默认，逐槽整合颗粒/PCB/主控）/ "rules"（旧整图规则识别）。
    """
    try:
        with _MANUAL_INSPECTION_LOCK:
            result = _capture_and_analyze(operator, mode, template_id, current_year, threshold,
                                          batch_id=None, save=False)
    except ValueError as e:                     # 模板缺失等
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
    return JSONResponse(result)
