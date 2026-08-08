# -*- coding: utf-8 -*-
"""海康 GigE 逐步计时诊断：绕开整个服务，纯原生 SDK 开一台相机抓一帧。

用法（项目目录）：
    .venv\\Scripts\\python.exe tools\\hik_probe.py            # 默认开 front(DB1224590)
    .venv\\Scripts\\python.exe tools\\hik_probe.py DB1623157   # 指定 SN

每个 SDK 调用前后打时间戳。哪一步耗时异常/卡住，一眼看出。
"""
import os
import sys
import time
from ctypes import POINTER, byref, cast, memset, sizeof

# 复用驱动里已配好的 SDK 路径与 import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.cameras import hik_camera as H  # noqa: E402

if not H.sdk_available():
    print("SDK 不可用：", H._IMPORT_ERR)
    sys.exit(1)

from MvCameraControl_class import *  # noqa: E402,F401,F403

TARGET_SN = (sys.argv[1] if len(sys.argv) > 1 else "DB1224590").strip()
t0 = time.time()


def step(msg):
    print(f"[+{time.time() - t0:6.2f}s] {msg}", flush=True)


step(f"开始，目标 SN={TARGET_SN}")

step("MV_CC_Initialize ...")
MvCamera.MV_CC_Initialize()
step("MV_CC_Initialize 完成")

dl = MV_CC_DEVICE_INFO_LIST()
types = (MV_GIGE_DEVICE | MV_USB_DEVICE)
step("MV_CC_EnumDevices ...")
r = MvCamera.MV_CC_EnumDevices(types, dl)
step(f"MV_CC_EnumDevices 完成 ret={hex(r & 0xFFFFFFFF)} 找到 {dl.nDeviceNum} 台")
if r != 0:
    sys.exit(1)

idx = None
for i in range(dl.nDeviceNum):
    info = cast(dl.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
    sn = H._decode(info.SpecialInfo.stGigEInfo.chSerialNumber)
    model = H._decode(info.SpecialInfo.stGigEInfo.chModelName)
    step(f"  设备[{i}] SN={sn} 型号={model}")
    if sn == TARGET_SN:
        idx = i
if idx is None:
    step(f"未找到 SN={TARGET_SN}")
    sys.exit(1)

st_dev = cast(dl.pDeviceInfo[idx], POINTER(MV_CC_DEVICE_INFO)).contents
cam = MvCamera()
step("MV_CC_CreateHandle ...")
r = cam.MV_CC_CreateHandle(st_dev)
step(f"MV_CC_CreateHandle 完成 ret={hex(r & 0xFFFFFFFF)}")

step("MV_CC_OpenDevice ...")
r = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
step(f"MV_CC_OpenDevice 完成 ret={hex(r & 0xFFFFFFFF)}")
if r != 0:
    cam.MV_CC_DestroyHandle()
    sys.exit(1)

step("GetOptimalPacketSize ...")
ps = cam.MV_CC_GetOptimalPacketSize()
step(f"GetOptimalPacketSize={ps}")
if int(ps) > 0:
    cam.MV_CC_SetIntValue("GevSCPSPacketSize", int(ps))
    step("SetIntValue GevSCPSPacketSize 完成")

# --- 还原服务 open() 里的 GigE 配置段，逐步计时找卡点 ---
if int(os.environ.get("HIK_RESEND", "1") or 0):
    pct = int(os.environ.get("HIK_RESEND_PERCENT", "100") or 100)
    tmo = int(os.environ.get("HIK_RESEND_TIMEOUT_MS", "50") or 50)
    step("MV_GIGE_SetResend ...")
    rr = cam.MV_GIGE_SetResend(1, pct, tmo)
    step(f"MV_GIGE_SetResend 完成 ret={hex(rr & 0xFFFFFFFF)}")

cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
step("TriggerMode=OFF")

fps = float(os.environ.get("HIK_FPS", "5") or 0)
if fps > 0:
    step(f"设帧率 {fps} ...")
    cam.MV_CC_SetBoolValue("AcquisitionFrameRateEnable", True)
    cam.MV_CC_SetFloatValue("AcquisitionFrameRate", fps)
    step("设帧率 完成")

step("设曝光 ExposureAuto=Off ...")
cam.MV_CC_SetEnumValueByString("ExposureAuto", "Off")
cam.MV_CC_SetFloatValue("ExposureTime", 46465.0)
step("设曝光 完成")
step("设增益 GainAuto=Off ...")
cam.MV_CC_SetEnumValueByString("GainAuto", "Off")
cam.MV_CC_SetFloatValue("Gain", 6.53)
step("设增益 完成")

step("SetImageNodeNum(6) ...")
cam.MV_CC_SetImageNodeNum(6)
step("SetImageNodeNum 完成")

step("MV_CC_StartGrabbing ...")
r = cam.MV_CC_StartGrabbing()
step(f"MV_CC_StartGrabbing 完成 ret={hex(r & 0xFFFFFFFF)}")
if r != 0:
    cam.MV_CC_CloseDevice(); cam.MV_CC_DestroyHandle()
    sys.exit(1)

frame = MV_FRAME_OUT()
memset(byref(frame), 0, sizeof(frame))
step("MV_CC_GetImageBuffer(timeout=3000) ...")
r = cam.MV_CC_GetImageBuffer(frame, 3000)
step(f"MV_CC_GetImageBuffer 完成 ret={hex(r & 0xFFFFFFFF)} pBuf={bool(frame.pBufAddr)}")
if r == 0 and frame.pBufAddr:
    fi = frame.stFrameInfo
    step(f"[OK] 抓到一帧 {fi.nWidth}x{fi.nHeight} 帧号={fi.nFrameNum}")
    cam.MV_CC_FreeImageBuffer(frame)
else:
    step("[FAIL] 取帧失败")

cam.MV_CC_StopGrabbing()
cam.MV_CC_CloseDevice()
cam.MV_CC_DestroyHandle()
step("已清理，结束")
