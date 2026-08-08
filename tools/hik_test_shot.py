# -*- coding: utf-8 -*-
"""走**项目自己的驱动**(hik_sdk)抓一张图并报告成像指标，验证 .env 参数是否被正确沿用。

用法：
    .venv\\Scripts\\python.exe tools\\hik_test_shot.py

会打印：实际生效的曝光/增益（对比 MVS 设的值，确认没被覆盖）、分辨率、
亮度均值/过曝欠曝比例/清晰度(拉普拉斯方差)，并存图到 test_photos/_shot_test.jpg。
注意：MVS 客户端若正连着相机会独占，先在 MVS 里断开。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.cameras import hik_sdk as H  # noqa: E402

if not H.sdk_available():
    print("SDK 不可用：", H._IMPORT_ERR)
    sys.exit(1)

print("枚举设备：")
for d in H.enum_devices():
    print("  ", d)

t0 = time.time()
cam = H.get_camera("front")
arr = cam.grab_array(timeout_ms=5000)
print(f"\n抓帧耗时 {time.time() - t0:.2f}s")

exp = cam.get_exposure()
gain = cam.get_gain()
print(f"生效曝光 = {exp}")
print(f"生效增益 = {gain}")
print(f"（MVS 里设的是 曝光 10000µs / 增益 0dB —— 两者应一致，不一致说明被 .env 覆盖了）")

h, w = arr.shape[:2]
g = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY) if arr.ndim == 3 else arr
mean = float(g.mean())
over = float((g >= 250).mean() * 100)
under = float((g <= 5).mean() * 100)
sharp = float(cv2.Laplacian(g, cv2.CV_64F).var())
print(f"\n分辨率 = {w}x{h}")
print(f"亮度均值 = {mean:.1f}   (过暗<40 / 偏亮>200；全黑≈0 说明曝光丢了)")
print(f"过曝像素 = {over:.2f}%   欠曝像素 = {under:.2f}%")
print(f"清晰度(拉普拉斯方差) = {sharp:.1f}   (越大越清晰；<100 通常是虚焦)")

out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_photos")
os.makedirs(out_dir, exist_ok=True)
out = os.path.join(out_dir, "_shot_test.jpg")
cv2.imwrite(out, arr, [cv2.IMWRITE_JPEG_QUALITY, 92])
print(f"\n已存图：{out}")
