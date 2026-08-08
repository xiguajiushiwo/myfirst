"""FastAPI 入口：创建 app、挂静态资源、建库、注册各路由分组。

工作流：选型号模板 → 上传/相机取 正/背面原图 + PCB/主控特写 →
        日期识别 + 外观质检（Qwen-VL）→ 清晰标注 → 返回结果（含分项计时）。

接口按职责拆到 `app/routers/`：recognition（识别/模板/文件夹/质检）、
cameras（UVC + 海康）、records（记录/操作人）、pipeline（自动流水线）；
共享业务逻辑在 `app/services.py`，常量/路径在 `app/core.py`。

启动: .venv\\Scripts\\python.exe -m uvicorn app.server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, metrics
from .core import ARCHIVE_DIR, OUTPUT_DIR, SLOT_LABELS, UPLOAD_DIR, WEB_DIR
from .logging_setup import get_logger, setup_logging
from .recognition import ocr_engine, template_store
from .storage import db
from .cameras import hik_camera as hik
from .routers import (auth as auth_router, batches, cameras, pipeline, records,
                      recognition, settings as settings_router)

setup_logging()
log = get_logger("yxq.server")

# 环境变量：OCR_DEVICE=cpu 切 CPU；OCR_SERVER_MODELS=0 用轻量模型(更快)；
#           OCR_DET_SIDE_LEN 调检测分辨率(默认1536，越大小字越准但越慢)
ocr_engine.configure(
    device=os.environ.get("OCR_DEVICE", "gpu"),
    lang=os.environ.get("OCR_LANG", "en"),
    use_server_models=os.environ.get("OCR_SERVER_MODELS", "1") == "1",
    det_limit_side_len=int(os.environ.get("OCR_DET_SIDE_LEN", "1536")),
    tile_bands=int(os.environ.get("OCR_TILE_BANDS", "1")),
)

app = FastAPI(title="云小圈AI硬件质检系统", version="4.0")

# 启动即建库建表（DB 不可用时不阻断服务，仅记录）
_DB_OK = False
try:
    db.init_db()
    _DB_OK = True
except Exception as e:  # noqa: BLE001
    log.warning("MySQL 初始化失败，质检记录相关功能不可用：%s", e)

app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/archive", StaticFiles(directory=ARCHIVE_DIR), name="archive")  # 追溯：永久归档图


@app.middleware("http")
async def _access_log(request: Request, call_next):
    """请求日志：方法/路径/状态/耗时。流式(SSE/预览)也只记一行起始，不阻塞。"""
    t0 = time.perf_counter()
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("请求异常 %s %s", request.method, request.url.path)
        raise
    dt = (time.perf_counter() - t0) * 1000
    # 静态与高频轮询降噪：只在慢或非 200 时记
    p = request.url.path
    noisy = p.startswith(("/uploads", "/outputs", "/api/folder/list")) or p in ("/api/pipeline/status",)
    if not noisy or resp.status_code >= 400 or dt > 1000:
        log.info("%s %s → %s %.0fms", request.method, p, resp.status_code, dt)
    return resp


# 无需登录即可访问的路径（入口页 + 登录/注册接口 + 静态图标/样式 + 健康检查）
_PUBLIC_PATHS = {"/login", "/api/login", "/api/register", "/api/me", "/app.css",
                 "/logo.png", "/logo_hd.png", "/logo_mark.png", "/favicon.ico", "/favicon.png", "/api/health"}
# 仅管理员可访问的页面 / 接口前缀（质检员被挡）
_ADMIN_PAGES = {"/manage", "/settings", "/users"}
_ADMIN_API_PREFIX = "/api/users"


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    """登录门禁：未登录 → 页面跳 /login、接口 401；质检员访问管理员页/接口 → 挡回。"""
    p = request.url.path
    if p in _PUBLIC_PATHS:
        return await call_next(request)
    user = auth.current_user(request)
    if not user:
        if p.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        return RedirectResponse("/login", status_code=302)
    if user.get("role") != "admin":
        if p in _ADMIN_PAGES:
            return RedirectResponse("/", status_code=302)
        if p.startswith(_ADMIN_API_PREFIX):
            return JSONResponse({"ok": False, "error": "需要管理员权限"}, status_code=403)
    return await call_next(request)


# --------------------- 页面 / 静态 / 健康检查 ---------------------

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


def _page(name: str) -> FileResponse:
    """HTML 页面：始终禁用缓存，避免改了前端浏览器还显示旧页面。"""
    return FileResponse(os.path.join(WEB_DIR, name), headers=_NO_CACHE)


@app.get("/login")
def login_page():
    """系统入口页：登录 + 注册（两功能并列）。未登录一律先到这。"""
    return _page("login.html")


@app.get("/users")
def users_page():
    """用户管理（管理员）：审核待通过账号 + 全部用户。"""
    return _page("users.html")


@app.get("/")
def index():
    """登录后落地：功能中心首页（一个个功能入口卡片）。"""
    return _page("home.html")


@app.get("/workbench")
def workbench():
    """质检工作台：双相机采集 + 识别 + 逐颗判定 + 入库（原首页）。"""
    return _page("index.html")


@app.get("/orders")
def orders_page():
    """采购订单页：从 OA 拉取/手工登记 → 卡片展示订单信息 → 选一张开始质检。"""
    return _page("orders.html")


@app.get("/camera")
def camera_page():
    """相机调试：双预览 + 每台曝光/增益/翻转/方向校正（参数服务端全局，工作台自动生效）。"""
    return _page("camera.html")


@app.get("/operators")
def operators_page():
    """质检员管理：新增/删除质检员（记录署名用）。质检前在工作台先确认质检员。"""
    return _page("operators.html")


@app.get("/manage")
def manage():
    """质检管理独立页：良品率看板（今日/累计/按客户/按批次）+ 记录明细。"""
    return _page("manage.html")


@app.get("/settings")
def settings_page():
    """系统设置：多模态大模型配置 + 用量。"""
    return _page("settings.html")


@app.get("/app.css")
def app_css():
    return FileResponse(os.path.join(WEB_DIR, "app.css"), media_type="text/css")


@app.get("/logo.png")
def logo():
    return FileResponse(os.path.join(WEB_DIR, "logo.png"))


@app.get("/logo_hd.png")
def logo_hd():
    """登录页中央用的高清 logo（比 topbar 的小 logo.png 清晰）。"""
    return FileResponse(os.path.join(WEB_DIR, "logo_hd.png"))


@app.get("/logo_mark.png")
def logo_mark():
    """纯云朵图标(无文字)高清版，顶栏 logo 用（官网 808×755 裁切）。"""
    return FileResponse(os.path.join(WEB_DIR, "logo_mark.png"))


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(WEB_DIR, "favicon.png"))


@app.get("/favicon.png")
def favicon_png():
    return FileResponse(os.path.join(WEB_DIR, "favicon.png"))


@app.get("/api/health")
def health():
    """健康检查：各子系统状态，供监控/告警轮询。"""
    # DB：轻量探活（重新试一次连接，反映当前而非启动时）
    db_ok = False
    try:
        db.list_operators()
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    # 海康相机：能否枚举到（不打开、不独占）
    try:
        hik_devs = len(hik.status().get("devices", []))
    except Exception:  # noqa: BLE001
        hik_devs = 0
    return {
        "status": "ok",
        "version": app.version,
        "slots": list(SLOT_LABELS.keys()),
        "templates": len(template_store.list_templates()),
        "db_ok": db_ok,
        "hik_cameras": hik_devs,
        "dashscope_key": bool(os.environ.get("DASHSCOPE_API_KEY", "").strip()),
    }


@app.get("/api/metrics")
def api_metrics():
    """运行指标：累计处理数、良率、错误数、运行时长（内存级，重启清零）。"""
    return metrics.snapshot()


# --------------------- 注册路由分组 ---------------------

app.include_router(auth_router.router)
app.include_router(recognition.router)
app.include_router(cameras.router)
app.include_router(records.router)
app.include_router(pipeline.router)
app.include_router(batches.router)
app.include_router(settings_router.router)


# ---- 相机子进程：主服务启动即拉起并监督，退出时回收（相机 SDK 隔离在子进程，卡死可秒级重启）----
from .cameras import cam_supervisor


@app.on_event("startup")
def _start_camera_service():
    if os.environ.get("CAM_SUPERVISOR", "1") != "1":     # 置 0 可关掉自动拉起(调试/无相机机器)
        log.info("相机子进程监督已禁用（CAM_SUPERVISOR=0）")
        return
    try:
        cam_supervisor.start()
    except Exception as e:  # noqa: BLE001
        log.warning("拉起相机子进程失败：%s", e)


@app.on_event("startup")
def _start_order_sync():
    batches.start_order_sync()


@app.on_event("shutdown")
def _stop_camera_service():
    try:
        cam_supervisor.stop()
    except Exception as e:  # noqa: BLE001
        log.warning("回收相机子进程失败：%s", e)


@app.on_event("shutdown")
def _stop_order_sync():
    batches.stop_order_sync()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
