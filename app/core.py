"""共享配置：路径常量、上传槽位定义、判定阈值。

被服务层 `services.py` 与各 `routers/*` 复用，避免散落在 server.py 里。
"""
from __future__ import annotations

import os

# ---- 目录 ----
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")     # 临时：上传/抓拍原图（用户会清理）
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")     # 临时：标注图
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")    # 永久：随质检记录归档的原图+标注图（追溯用）
WEB_DIR = os.path.join(BASE_DIR, "web")
TEST_DIR = os.path.join(BASE_DIR, "test_photos")     # 测试用：把照片放这里直接读
# 相机输出目录：相机每拍一根内存条自动生成一个子文件夹(含正反两张)，系统监听自动识别
WATCH_DIR = os.environ.get("WATCH_DIR", TEST_DIR)
for _d in (UPLOAD_DIR, OUTPUT_DIR, ARCHIVE_DIR, TEST_DIR, WATCH_DIR):
    os.makedirs(_d, exist_ok=True)

# ---- 四个上传槽：正/背面只识别存储颗粒；PCB/主控为单独特写照片 ----
SLOT_LABELS = {
    "front": "正面颗粒",
    "back": "背面颗粒",
    "pcb": "PCB 板",
    "controller": "主控芯片",
}
# kind=side 用固定框区域识别；kind=chip 用整图识别
SLOT_KIND = {"front": "side", "back": "side", "pcb": "chip", "controller": "chip"}

# 合格判据：所有日期的最大周差阈值（含）
SPREAD_THRESHOLD_WEEKS = 10

# 可识别的图片扩展名
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
