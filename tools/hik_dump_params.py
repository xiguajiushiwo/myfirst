# -*- coding: utf-8 -*-
"""读出相机**当前**关键参数（你在 MVS 里调好的值），只读、不修改任何设置。

用法（项目目录）：
    .venv\\Scripts\\python.exe tools\\hik_dump_params.py            # 枚举到的第一台
    .venv\\Scripts\\python.exe tools\\hik_dump_params.py DB1858063   # 指定 SN

注意：MVS 客户端若正连着相机会独占，需先在 MVS 里断开该相机再跑本脚本。
输出 JSON 便于同步进 .env / config/camera_params.json。
"""
import json
import os
import sys
from ctypes import POINTER, cast

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.cameras import hik_sdk as H  # noqa: E402  (SDK 在子进程模块里，hik_camera 只是瘦客户端)

if not H.sdk_available():
    print("SDK 不可用：", H._IMPORT_ERR)
    sys.exit(1)

from MvCameraControl_class import *  # noqa: E402,F401,F403

TARGET_SN = (sys.argv[1] if len(sys.argv) > 1 else "").strip()

MvCamera.MV_CC_Initialize()
dl = MV_CC_DEVICE_INFO_LIST()
r = MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, dl)
if r != 0:
    print(f"枚举失败 ret={hex(r & 0xFFFFFFFF)}")
    sys.exit(1)
print(f"枚举到 {dl.nDeviceNum} 台相机")

found = []
for i in range(dl.nDeviceNum):
    info = cast(dl.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
    if info.nTLayerType == MV_USB_DEVICE:
        spec, kind = info.SpecialInfo.stUsb3VInfo, "USB"
    else:
        spec, kind = info.SpecialInfo.stGigEInfo, "GigE"
    sn = H._decode(spec.chSerialNumber)
    model = H._decode(spec.chModelName)
    print(f"  [{i}] 接口={kind} SN={sn} 型号={model}")
    found.append((i, sn, kind, model))

if not found:
    sys.exit(1)

idx, sn, kind, model = found[0]
if TARGET_SN:
    hit = [f for f in found if f[1] == TARGET_SN]
    if not hit:
        print(f"未找到 SN={TARGET_SN}")
        sys.exit(1)
    idx, sn, kind, model = hit[0]

st_dev = cast(dl.pDeviceInfo[idx], POINTER(MV_CC_DEVICE_INFO)).contents
cam = MvCamera()
if cam.MV_CC_CreateHandle(st_dev) != 0:
    print("CreateHandle 失败")
    sys.exit(1)
r = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
if r != 0:
    print(f"OpenDevice 失败 ret={hex(r & 0xFFFFFFFF)}  → 相机可能被 MVS 独占，请在 MVS 里断开该相机")
    cam.MV_CC_DestroyHandle()
    sys.exit(1)


def f_val(name):
    v = MVCC_FLOATVALUE()
    return round(v.fCurValue, 3) if cam.MV_CC_GetFloatValue(name, v) == 0 else None


def i_val(name):
    v = MVCC_INTVALUE()
    return int(v.nCurValue) if cam.MV_CC_GetIntValue(name, v) == 0 else None


def b_val(name):
    v = c_bool(False)
    return bool(v.value) if cam.MV_CC_GetBoolValue(name, v) == 0 else None


def e_str(name):
    v = MVCC_ENUMVALUE()
    if cam.MV_CC_GetEnumValue(name, v) != 0:
        return None
    s = MVCC_ENUMENTRY()
    s.nValue = v.nCurValue
    if cam.MV_CC_GetEnumEntrySymbolic(name, s) == 0:
        return H._decode(s.chSymbolic) or int(v.nCurValue)
    return int(v.nCurValue)


out = {
    "sn": sn,
    "interface": kind,
    "model": model,
    "exposure_us": f_val("ExposureTime"),
    "gain_db": f_val("Gain"),
    "exposure_auto": e_str("ExposureAuto"),
    "gain_auto": e_str("GainAuto"),
    "black_level": i_val("BlackLevel"),
    "gamma": f_val("Gamma"),
    "gamma_enable": b_val("GammaEnable"),
    "width": i_val("Width"),
    "height": i_val("Height"),
    "offset_x": i_val("OffsetX"),
    "offset_y": i_val("OffsetY"),
    "pixel_format": e_str("PixelFormat"),
    "frame_rate_enable": b_val("AcquisitionFrameRateEnable"),
    "frame_rate": f_val("AcquisitionFrameRate"),
    "resulting_frame_rate": f_val("ResultingFrameRate"),
    "trigger_mode": e_str("TriggerMode"),
    "packet_size": i_val("GevSCPSPacketSize"),
    "packet_delay": i_val("GevSCPD"),
    "balance_white_auto": e_str("BalanceWhiteAuto"),
    "reverse_x": b_val("ReverseX"),
    "reverse_y": b_val("ReverseY"),
    "sharpness": i_val("Sharpness"),
    "sharpness_enable": b_val("SharpnessEnable"),
    "device_temperature": f_val("DeviceTemperature"),
}

cam.MV_CC_CloseDevice()
cam.MV_CC_DestroyHandle()

print("\n=== 相机当前参数（MVS 里的实际值）===")
print(json.dumps(out, ensure_ascii=False, indent=2))
