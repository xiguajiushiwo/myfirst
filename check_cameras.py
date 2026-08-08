# -*- coding: utf-8 -*-
"""相机自检：在【插着相机的服务器】上跑，看 Python(OpenCV) 能不能直接管相机。

用法（在项目目录）：
    .venv\\Scripts\\python.exe check_cameras.py

它会依次尝试打开设备序号 0~5，能打开的就抓一帧存成 cam_test_<序号>.jpg，
并打印分辨率。跑完看输出和存出的图：
  - 有序号能打开、存出的图是相机画面   → Python 能直接管，用「相机采集(A)」，
    把能用的序号填进 .env 的 FRONT_CAM_INDEX / BACK_CAM_INDEX。
  - 全部打不开(但相机在它自带软件里能用)  → 多半是别的程序占用/非标准UVC，
    先关掉其它用相机的软件再跑；仍不行就走「读文件夹(B)」。

注意：UVC 相机同一时刻只能被一个程序打开——测试前请先关掉相机自带软件/其它占用。
"""
import time

import cv2

MAX_INDEX = 6          # 试 0~5
TRY_W, TRY_H = 3840, 2160   # 试着拉到 4K，报告实际生效分辨率
WARMUP = 20            # 先丢掉前若干帧让相机热身（自动曝光），避免首帧全黑

print("开始检测相机（0~%d）...\n" % (MAX_INDEX - 1))
found = []
for i in range(MAX_INDEX):
    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)   # Windows 用 DirectShow 后端
    if not cap or not cap.isOpened():
        print(f"[序号 {i}] 打不开")
        if cap:
            cap.release()
        continue
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, TRY_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, TRY_H)
    ok, frame = False, None
    for _ in range(WARMUP):        # 热身：连续读几帧，取最后一帧（避开全黑首帧）
        ok, frame = cap.read()
        time.sleep(0.05)
    if ok and frame is not None:
        h, w = frame.shape[:2]
        out = f"cam_test_{i}.jpg"
        cv2.imwrite(out, frame)
        print(f"[序号 {i}] ✅ 打开成功，分辨率 {w}x{h}，已存样图 {out}")
        found.append((i, w, h))
    else:
        print(f"[序号 {i}] 打开了但抓不到画面")
    cap.release()

print("\n=== 结果 ===")
if found:
    print("可用相机序号：", [i for i, _, _ in found])
    print("→ Python 能直接管相机，走「相机采集」。把序号填进 .env：")
    idxs = [i for i, _, _ in found]
    print(f"   FRONT_CAM_INDEX={idxs[0]}")
    if len(idxs) > 1:
        print(f"   BACK_CAM_INDEX={idxs[1]}")
    print("打开存出的 cam_test_*.jpg 看看是不是对应的正/背面画面，确定哪台是正面哪台是背面。")
else:
    print("没有能直接打开的相机。")
    print("→ 先关掉相机自带软件/其它占用再跑一次；仍不行就走「读文件夹」方案。")
