# -*- coding: utf-8 -*-
"""双相机带宽/帧率压测：把帧率拉满，连续取流统计 实际FPS + 实际吞吐(MB/s)。
用法：先调 /api/hik/release 释放相机，再 .venv 跑本脚本。只读取帧、不落盘。
"""
import os, sys, time, threading
from ctypes import POINTER, byref, cast, sizeof, memset

RUNENV = os.environ.get("MVCAM_COMMON_RUNENV", r"E:\MVS\Development")
sys.path.append(os.path.join(RUNENV, "Samples", "Python", "MvImport"))
from MvCameraControl_class import *  # noqa

MV_GIGE = MV_GIGE_DEVICE
SECS = float(os.environ.get("BENCH_SECS", "6"))


def enum():
    dl = MV_CC_DEVICE_INFO_LIST()
    memset(byref(dl), 0, sizeof(dl))
    MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, dl)
    out = []
    for i in range(dl.nDeviceNum):
        info = cast(dl.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
        g = info.SpecialInfo.stGigEInfo
        out.append((i, info, bytes(g.chSerialNumber).split(b"\x00")[0].decode("ascii", "ignore"),
                    bytes(g.chModelName).split(b"\x00")[0].decode("ascii", "ignore")))
    return out


def open_cam(info):
    cam = MvCamera()
    assert cam.MV_CC_CreateHandle(info) == 0, "CreateHandle"
    r = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    assert r == 0, f"OpenDevice ret=0x{r & 0xffffffff:08x}"
    ps = cam.MV_CC_GetOptimalPacketSize()
    if ps > 0:
        cam.MV_CC_SetIntValue("GevSCPSPacketSize", int(ps))
    # 关限速/关重传，测纯上限
    cam.MV_CC_SetEnumValue("TriggerMode", 0)
    # 帧率拉满：关闭帧率上限（enable=False → 按曝光/带宽能跑多快跑多快）
    try:
        cam.MV_CC_SetBoolValue("AcquisitionFrameRateEnable", False)
    except Exception:
        pass
    try:
        cam.MV_CC_SetImageNodeNum(8)
    except Exception:
        pass
    return cam, int(ps)


def bench(cam, sn, secs, hold):
    """hold: barrier event，让两台同时开始，测并发带宽。"""
    fr = MV_FRAME_OUT()
    memset(byref(fr), 0, sizeof(fr))
    # 先热一帧拿分辨率/payload
    frames = 0
    bytes_ = 0
    w = h = 0
    hold.wait()
    t0 = time.time()
    while time.time() - t0 < secs:
        r = cam.MV_CC_GetImageBuffer(fr, 1000)
        if r != 0:
            continue
        frames += 1
        bytes_ += fr.stFrameInfo.nFrameLen
        w, h = fr.stFrameInfo.nWidth, fr.stFrameInfo.nHeight
        cam.MV_CC_FreeImageBuffer(fr)
    dt = time.time() - t0
    fps = frames / dt if dt else 0
    mbps = bytes_ / dt / 1e6
    netmbps = mbps * 8  # 折算网络 Mb/s
    print(f"[{sn}] {w}x{h}  帧数={frames}  时长={dt:.1f}s  "
          f"实际FPS={fps:.1f}  吞吐={mbps:.0f} MB/s  ≈{netmbps:.0f} Mbps")
    return fps, netmbps


def run_on(cams, label):
    hold = threading.Event()
    res = {}
    ths = []
    for cam, sn in cams:
        t = threading.Thread(target=lambda c=cam, s=sn: res.__setitem__(s, bench(c, s, SECS, hold)))
        t.start(); ths.append(t)
    time.sleep(0.3); hold.set()
    for t in ths: t.join()
    if len(cams) == 2:
        tot = sum(v[1] for v in res.values())
        print(f"  >> {label} 两台合计 ~ {tot:.0f} Mbps")
    print()


def main():
    devs = enum()
    print("发现相机：", [(d[2], d[3]) for d in devs])
    opened = []
    for i, info, sn, model in devs:
        cam, ps = open_cam(info)
        cam.MV_CC_StartGrabbing()
        opened.append((cam, sn))
        print(f"  打开 {sn} ({model}) 最佳包大小={ps}")
    print()
    # 1) 各自单跑（看单台上限）
    for cam, sn in opened:
        print(f"--- 单台压测 {sn} ---")
        run_on([(cam, sn)], sn)
    # 2) 两台并发（看共线合计上限）
    if len(opened) == 2:
        print("--- 两台并发压测 ---")
        run_on(opened, "并发")
    for cam, sn in opened:
        cam.MV_CC_StopGrabbing(); cam.MV_CC_CloseDevice(); cam.MV_CC_DestroyHandle()


if __name__ == "__main__":
    main()
