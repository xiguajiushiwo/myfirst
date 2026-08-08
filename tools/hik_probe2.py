# -*- coding: utf-8 -*-
"""海康双相机并发诊断：两个线程同时开两台相机各抓一帧，复现服务里的并发场景。

用法（项目目录）：
    .venv\\Scripts\\python.exe tools\\hik_probe2.py

每步带线程名+时间戳。若并发时某步卡住/变慢，一眼看出。
"""
import os
import sys
import threading
import time
from ctypes import POINTER, byref, cast, memset, sizeof

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.cameras import hik_camera as H  # noqa: E402

if not H.sdk_available():
    print("SDK 不可用：", H._IMPORT_ERR)
    sys.exit(1)

from MvCameraControl_class import *  # noqa: E402,F401,F403

SNS = ["DB1224590", "DB1623157"]
t0 = time.time()
_plock = threading.Lock()


def step(msg):
    with _plock:
        print(f"[+{time.time() - t0:6.2f}s] {msg}", flush=True)


MvCamera.MV_CC_Initialize()
dl = MV_CC_DEVICE_INFO_LIST()
r = MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, dl)
step(f"EnumDevices ret={hex(r & 0xFFFFFFFF)} 找到 {dl.nDeviceNum} 台")

# SN -> device info 索引
sn2idx = {}
for i in range(dl.nDeviceNum):
    info = cast(dl.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
    sn2idx[H._decode(info.SpecialInfo.stGigEInfo.chSerialNumber)] = i


def open_grab(sn):
    tag = sn
    idx = sn2idx.get(sn)
    if idx is None:
        step(f"[{tag}] 未找到")
        return
    st_dev = cast(dl.pDeviceInfo[idx], POINTER(MV_CC_DEVICE_INFO)).contents
    cam = MvCamera()
    step(f"[{tag}] CreateHandle ...")
    cam.MV_CC_CreateHandle(st_dev)
    step(f"[{tag}] OpenDevice ...")
    r = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    step(f"[{tag}] OpenDevice ret={hex(r & 0xFFFFFFFF)}")
    if r != 0:
        cam.MV_CC_DestroyHandle(); return
    step(f"[{tag}] GetOptimalPacketSize ...")
    ps = cam.MV_CC_GetOptimalPacketSize()
    step(f"[{tag}] PacketSize={ps}")
    if int(ps) > 0:
        cam.MV_CC_SetIntValue("GevSCPSPacketSize", int(ps))
    step(f"[{tag}] SetResend ...")
    cam.MV_GIGE_SetResend(1, 100, 50)
    cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
    cam.MV_CC_SetBoolValue("AcquisitionFrameRateEnable", True)
    cam.MV_CC_SetFloatValue("AcquisitionFrameRate", 15.0)
    step(f"[{tag}] StartGrabbing ...")
    r = cam.MV_CC_StartGrabbing()
    step(f"[{tag}] StartGrabbing ret={hex(r & 0xFFFFFFFF)}")
    if r != 0:
        cam.MV_CC_CloseDevice(); cam.MV_CC_DestroyHandle(); return
    for n in range(3):
        frame = MV_FRAME_OUT()
        memset(byref(frame), 0, sizeof(frame))
        step(f"[{tag}] GetImageBuffer #{n} ...")
        r = cam.MV_CC_GetImageBuffer(frame, 3000)
        if r == 0 and frame.pBufAddr:
            fi = frame.stFrameInfo
            step(f"[{tag}] #{n} OK {fi.nWidth}x{fi.nHeight} 帧号={fi.nFrameNum}")
            cam.MV_CC_FreeImageBuffer(frame)
        else:
            step(f"[{tag}] #{n} 取帧失败 ret={hex(r & 0xFFFFFFFF)}")
    cam.MV_CC_StopGrabbing()
    cam.MV_CC_CloseDevice()
    cam.MV_CC_DestroyHandle()
    step(f"[{tag}] 清理完成")


ts = [threading.Thread(target=open_grab, args=(sn,)) for sn in SNS]
for t in ts:
    t.start()
for t in ts:
    t.join()
step("全部结束")
