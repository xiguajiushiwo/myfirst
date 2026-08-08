"""海康 / 海康机器人(Hikrobot) MVS 工业相机驱动。

封装 MVS Python SDK（`%MVCAM_COMMON_RUNENV%\\Samples\\Python\\MvImport`），
对外提供极简接口：枚举、打开并持续取流、按需抓一帧存 JPG、关闭。

设计：
- **单相机**场景（如 MV-CS050-10GC）。内存条正/反面由操作员翻面分两次拍。
- 相机打开后**持续取流**，`snapshot()` 抓当前最新帧存文件（内部用
  `MV_CC_SaveImageToFileEx2` 直接把原始像素转好并写盘，省去手工像素转换）。
- SDK 未安装 / 相机未连接时**优雅报错**（抛 RuntimeError），不影响系统其余部分。
- 线程安全：`snapshot` 加锁，供 FastAPI 多请求复用同一相机。

环境变量（可选）：
- `HIK_CAM_SN`   指定相机序列号（多相机时精确选中；缺省用第 0 台）
- `HIK_CAM_INDEX` 指定枚举序号（缺省 0）
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from ctypes import POINTER, byref, c_ubyte, cast, memset, sizeof

log = logging.getLogger("yxq.cam.sdk")

from . import param_store

# 开机判定相机曝光"默认/丢失"的阈值(µs)：海康出厂默认约 5000µs，正常好值 4~5 万。
# 开机读到相机当前曝光 ≤ 此值 → 判定丢值，回退持久化备份；> 此值 → 采用相机当前值(MVS调的)并同步入备份。
_LOST_EXP_THRESHOLD_US = float(os.environ.get("HIK_LOST_EXP_US", "10000") or 10000)

# ---- 载入 MVS Python 绑定（DLL 路径由安装时写入系统 PATH / GENICAM 环境变量）----
_RUNENV_CANDIDATES = [
    os.environ.get("MVCAM_COMMON_RUNENV", ""),
    r"C:\MVS\Development",
    r"C:\Program Files\MVS\Development",
    r"C:\Program Files (x86)\MVS\Development",
    r"E:\MVS\Development",
]
_RUNENV = next((path for path in _RUNENV_CANDIDATES
                if path and os.path.isfile(os.path.join(
                    path, "Samples", "Python", "MvImport", "MvCameraControl_class.py"))),
               _RUNENV_CANDIDATES[0] or r"C:\MVS\Development")
_MVIMPORT = os.path.join(_RUNENV, "Samples", "Python", "MvImport")
_DLL_DIR_CANDIDATES = [
    os.environ.get("MVCAM_COMMON_RUNENV_64", ""),
    r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64",
    r"C:\Program Files\Common Files\MVS\Runtime\Win64_x64",
    os.path.join(_RUNENV, "Runtime", "Win64_x64"),
]
_DLL_DIR = next((path for path in _DLL_DIR_CANDIDATES
                 if path and os.path.isfile(os.path.join(path, "MvCameraControl.dll"))), "")
_DLL_HANDLE = None
if _DLL_DIR:
    os.environ["PATH"] = _DLL_DIR + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        _DLL_HANDLE = os.add_dll_directory(_DLL_DIR)

_SDK_OK = False
_IMPORT_ERR = ""
try:
    if os.path.isdir(_MVIMPORT) and _MVIMPORT not in sys.path:
        sys.path.append(_MVIMPORT)
    from MvCameraControl_class import *  # noqa: F401,F403
    from PixelType_header import PixelType_Gvsp_BGR8_Packed  # 目标像素格式
    _SDK_OK = True
except Exception as e:  # noqa: BLE001
    _IMPORT_ERR = f"{type(e).__name__}: {e}"

# 全局"打开"锁：串行化 枚举+创建句柄+打开设备（海康 EnumDevices 非线程安全，双相机并发打开会互相清枚举）
_open_lock = threading.Lock()

# 枚举结果缓存：**只枚举一次**。海康 GigE 一台相机在取流后，再调 EnumDevices 往往拿不到
# 另一台（→"未找到 SN"）；所以缓存首次(无相机打开时)的设备表，两台都从缓存按 SN 找、不再重枚举。
_dev_list = None
_DEV_TYPES = None


def _ensure_enum(force: bool = False):
    """返回设备表(缓存)。force=True 时重新枚举。调用方需持有 _open_lock（避免并发枚举）。"""
    global _dev_list, _DEV_TYPES
    if _dev_list is not None and not force:
        return _dev_list
    MvCamera.MV_CC_Initialize()
    if _DEV_TYPES is None:
        _DEV_TYPES = (MV_GIGE_DEVICE | MV_USB_DEVICE | MV_GENTL_CAMERALINK_DEVICE
                      | MV_GENTL_CXP_DEVICE | MV_GENTL_XOF_DEVICE)
    dl = MV_CC_DEVICE_INFO_LIST()
    if MvCamera.MV_CC_EnumDevices(_DEV_TYPES, dl) != 0:
        raise RuntimeError("枚举设备失败")
    _dev_list = dl
    return _dev_list


def _err(code: int) -> str:
    return "0x%x" % (code & 0xFFFFFFFF)


def _decode(char_array) -> str:
    b = memoryview(char_array).tobytes()
    i = b.find(b"\x00")
    if i >= 0:
        b = b[:i]
    for enc in ("gbk", "utf-8", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1", "replace")


def sdk_available() -> bool:
    return _SDK_OK


def enum_devices() -> list[dict]:
    """枚举当前连接的相机，返回 [{index, model, sn, ip, type}]。SDK 不可用则抛错。

    有相机已打开时用**缓存**设备表（GigE 相机在流时重枚举会拿不到另一台/扰动取流）；
    无相机打开时刷新枚举。整个过程持 `_open_lock`，避免与打开时的枚举并发。
    """
    if not _SDK_OK:
        raise RuntimeError(f"MVS SDK 未就绪：{_IMPORT_ERR}（MvImport={_MVIMPORT}）")
    with _open_lock:
        dl = _ensure_enum(force=not _cams)         # 无相机打开→刷新；有打开→用缓存
    out = []
    for i in range(dl.nDeviceNum):
        info = cast(dl.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
        if info.nTLayerType in (MV_GIGE_DEVICE, MV_GENTL_GIGE_DEVICE):
            g = info.SpecialInfo.stGigEInfo
            ip = g.nCurrentIp
            out.append({"index": i, "type": "GigE",
                        "model": _decode(g.chModelName), "sn": _decode(g.chSerialNumber),
                        "ip": "%d.%d.%d.%d" % ((ip >> 24) & 0xFF, (ip >> 16) & 0xFF,
                                               (ip >> 8) & 0xFF, ip & 0xFF)})
        elif info.nTLayerType == MV_USB_DEVICE:
            u = info.SpecialInfo.stUsb3VInfo
            out.append({"index": i, "type": "USB",
                        "model": _decode(u.chModelName), "sn": _decode(u.chSerialNumber), "ip": ""})
    return out


class HikCamera:
    """单台海康工业相机：open() 打开并持续取流；snapshot(path) 抓帧存 JPG；close()。"""

    def __init__(self, index: int | None = None, sn: str | None = None, orient: str = "none",
                 exp_us: float | None = None, gain_db: float | None = None, role: str = ""):
        self.index = int(os.environ.get("HIK_CAM_INDEX", "0")) if index is None else index
        self.sn = (os.environ.get("HIK_CAM_SN", "") if sn is None else sn).strip()
        self.role = role                        # front/back，供曝光增益持久化按角色存
        self.orient = orient or "none"          # 方向校正模式（反面翻正，见 _apply_orient）
        self.exp_us = exp_us                    # 开机自动应用的曝光(µs)/增益(dB)，None=不设
        self.gain_db = gain_db
        self.cam = None
        self.info = None
        self.last_ok = 0.0                      # 最近一次成功取帧时间戳（看门狗判活）
        self.opened_ts = 0.0                    # 最近一次成功打开取流的时间（看门狗热身宽限）
        self._last_recover = 0.0                # 最近一次自愈重连时间（冷却，防多消费者重连风暴）
        self._recover_lock = threading.Lock()
        self._lock = threading.Lock()

    # ---- 生命周期 ----
    def open(self):
        if self.cam is not None:
            return self
        if not _SDK_OK:
            raise RuntimeError(f"MVS SDK 未就绪：{_IMPORT_ERR}")
        # **枚举+创建句柄+打开设备 串行化**：海康 SDK 的 EnumDevices 用共享设备表、
        # 非线程安全；两台相机并发打开时会互相清掉对方的枚举结果（→"未找到 SN"）。
        # 抓帧(snapshot/grab)仍各自并发，不受此锁影响。
        with _open_lock:
            if self.cam is not None:                  # 双检：可能已被另一线程打开
                return self
            dl = _ensure_enum()                        # 用缓存设备表（不再每台重复枚举）
            if dl is None or dl.nDeviceNum == 0:
                dl = _ensure_enum(force=True)
            if dl is None or dl.nDeviceNum == 0:
                raise RuntimeError("未发现相机（检查网线/供电/占用：MVS 客户端是否正连着该相机）")

            def _find_idx(dlist):
                if not self.sn:
                    return self.index
                for i in range(dlist.nDeviceNum):
                    info = cast(dlist.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
                    if info.nTLayerType == MV_USB_DEVICE:
                        serial = _decode(info.SpecialInfo.stUsb3VInfo.chSerialNumber)
                    else:
                        serial = _decode(info.SpecialInfo.stGigEInfo.chSerialNumber)
                    if serial == self.sn:
                        return i
                return None

            idx = _find_idx(dl)
            if idx is None:                            # 缓存里没有 → 强制重枚举一次(热插拔兜底)
                dl = _ensure_enum(force=True)
                idx = _find_idx(dl)
                if idx is None:
                    raise RuntimeError(f"未找到 SN={self.sn} 的相机")
            if idx >= dl.nDeviceNum:
                raise RuntimeError(f"相机序号 {idx} 超出范围（共 {dl.nDeviceNum} 台）")

            st_dev = cast(dl.pDeviceInfo[idx], POINTER(MV_CC_DEVICE_INFO)).contents
            cam = MvCamera()
            r = cam.MV_CC_CreateHandle(st_dev)
            if r != 0:
                raise RuntimeError(f"创建句柄失败 ret={_err(r)}")
            r = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
            if r != 0:                                     # 释放相机后 GigE 控制通道有短暂释放窗口，重试 2 次覆盖
                for _ in range(2):                         # 只重试~1s：相机真被 MVS 独占时快速失败，
                    time.sleep(0.5)                        # 不再死等 9s×两台=18s 把 status/曝光/增益全拖垮
                    r = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
                    if r == 0:
                        break
            if r != 0:
                cam.MV_CC_DestroyHandle()
                raise RuntimeError(f"打开设备失败 ret={_err(r)}（可能被 MVS 客户端独占，请先在客户端断开）")
        # GigE：探测最佳包大小 + 包间延迟/限带宽（两台共用一条千兆线，防打满丢包掉线）
        if st_dev.nTLayerType in (MV_GIGE_DEVICE, MV_GENTL_GIGE_DEVICE):
            ps = cam.MV_CC_GetOptimalPacketSize()
            if int(ps) > 0:
                cam.MV_CC_SetIntValue("GevSCPSPacketSize", int(ps))

            def _try(fn):                          # 老固件个别节点可能没有，缺就跳过
                try:
                    fn()
                except Exception:
                    pass
            # GigE 丢包重传：SDK 发现丢包 → 请求相机重发缺失的包 → 拼出完整帧，直接消除“花屏/撕裂”。
            # 对 back 走的 USB-GbE 弱链路尤其关键。静态质检拍照，重传的微小延迟可接受。默认开(HIK_RESEND=0 关)。
            if int(os.environ.get("HIK_RESEND", "1") or 0):
                pct = int(os.environ.get("HIK_RESEND_PERCENT", "100") or 100)  # 允许重传的最大丢包比例(%)
                tmo = int(os.environ.get("HIK_RESEND_TIMEOUT_MS", "50") or 50)  # 单次重传等待(ms)
                _try(lambda: cam.MV_GIGE_SetResend(1, pct, tmo))
                rty = int(os.environ.get("HIK_RESEND_RETRY", "0") or 0)         # 最大重传次数(0=用SDK默认)
                if rty > 0:
                    _try(lambda: cam.MV_GIGE_SetResendMaxRetryTimes(rty))
            scpd = int(os.environ.get("HIK_GEVSCPD", "0") or 0)         # 包间延迟(ticks)，默认关(tick单位因机型而异易过冲)；主要靠限帧省带宽
            if scpd > 0:
                _try(lambda: cam.MV_CC_SetIntValue("GevSCPD", scpd))
            thr = int(os.environ.get("HIK_THROUGHPUT_BPS", "0") or 0)   # 每台吞吐上限(Bps)，0=不限
            if thr > 0:
                _try(lambda: cam.MV_CC_SetEnumValueByString("DeviceLinkThroughputLimitMode", "On"))
                _try(lambda: cam.MV_CC_SetIntValue("DeviceLinkThroughputLimit", thr))
        cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)   # 连续采集
        # 限采集帧率：静态托盘不需要高帧率，降帧=最直接省带宽（抓拍仍取最新帧，≤200ms 足够新）
        fps = float(os.environ.get("HIK_FPS", "5") or 0)
        if fps > 0:
            try:
                cam.MV_CC_SetBoolValue("AcquisitionFrameRateEnable", True)
                cam.MV_CC_SetFloatValue("AcquisitionFrameRate", fps)
            except Exception:
                pass
        self.cam = cam
        # 应用曝光/增益：**优先用相机当前值(你在 MVS 里调的)**，仅当相机丢值(断电回默认~5000µs)才回退持久化备份。
        # 判定：开机读相机当前曝光，> 阈值(默认10000µs)=正常 → 采用它并同步入备份(备份=最近好值)；
        #       ≤ 阈值=默认/丢失 → 用 param_store 里持久化的好值刷回硬件(避免全黑→静止即拍失灵)。
        # 曝光与增益联动：曝光判定丢失时两者一起回退（增益单独判无明确默认值，跟随曝光结论最稳）。
        # 注意：open() 可能在调用方持有 self._lock 时被调用（grab_array/snapshot 在锁内触发首次 open）。
        # self._lock 非重入，这里只调**不加锁**的 _set_*_locked / _get_*_locked（cam 已就绪，SDK 调用安全）。
        self._apply_boot_params()
        try:
            cam.MV_CC_SetImageNodeNum(int(os.environ.get("HIK_NODE_NUM", "6")))  # 多缓几帧，吸收突发防卡流
        except Exception:
            pass
        r = cam.MV_CC_StartGrabbing()
        if r != 0:
            cam.MV_CC_CloseDevice()
            cam.MV_CC_DestroyHandle()
            self.cam = None
            raise RuntimeError(f"开始取流失败 ret={_err(r)}")
        if st_dev.nTLayerType == MV_USB_DEVICE:
            dev_info = st_dev.SpecialInfo.stUsb3VInfo
        else:
            dev_info = st_dev.SpecialInfo.stGigEInfo
        self.info = {"model": _decode(dev_info.chModelName),
                     "sn": _decode(dev_info.chSerialNumber)}
        self.opened_ts = time.time()               # 热身起点（看门狗宽限用）
        return self

    def _recover(self):
        """取帧失败（掉线/流卡死）时自愈：关闭句柄再重开取流。设备仍在网即可自动恢复。

        GigE 独占句柄释放有延迟，close 后立刻 open 会撞 0x80000214，故多等几次再重开。
        **冷却门**：8s 内只重连一次——预览流/运动循环/看门狗可能同时发现卡流狂调本函数，
        没有冷却会 close/open 打架成"重连风暴"（曾导致一路预览饿死）。
        """
        with self._recover_lock:
            if time.time() - self._last_recover < 8:
                return                                 # 冷却中：别人刚重连过，跳过
            self._last_recover = time.time()
        try:
            self.close()
        except Exception:
            pass
        last = None
        for wait in (0.8, 1.5, 2.5):
            time.sleep(wait)
            try:
                self.open()
                return
            except Exception as e:  # noqa: BLE001
                last = e
        if last:
            log.warning("自愈重连失败：%s", last)

    # ---- 曝光 / 增益（代码层面可调） ----
    # 关键：曝光/增益的读写都必须持 self._lock —— 与 grab_array/snapshot 同一把锁。
    # 海康 MVS SDK 同一设备句柄**非线程安全**：控制命令(GetFloatValue)与取流(GetImageBuffer)
    # 若在同一 handle 上并发，SDK 内部会撞成一团——控制命令从 ~300ms 崩到 20-38s，取流线程
    # 也被拖住导致预览卡死不出图。加锁串行化后，控制命令最多等一帧取完(几十 ms)即可执行。
    def _set_exposure_locked(self, us: float):
        """实设曝光（不加锁）。调用方须已持 self._lock 或处于 open() 内（cam 已就绪）。"""
        self.cam.MV_CC_SetEnumValueByString("ExposureAuto", "Off")         # 关自动曝光
        r = self.cam.MV_CC_SetFloatValue("ExposureTime", float(us))
        if r != 0:
            raise RuntimeError(f"设置曝光失败 ret={_err(r)}")

    def set_exposure(self, us: float):
        """设置曝光时间（微秒）。先关自动曝光，再设固定值。返回实际生效范围/当前值。"""
        with self._lock:
            if self.cam is None:
                self.open()
            self._set_exposure_locked(us)
        if self.role:
            param_store.save_role(self.role, exp_us=us)     # 运行时改也更新备份(最近好值)
        return self.get_exposure()                                          # 锁外读回（get_exposure 自持锁）

    def get_exposure(self) -> dict:
        """读当前曝光时间（微秒）及可调范围。"""
        v = MVCC_FLOATVALUE()
        memset(byref(v), 0, sizeof(v))
        with self._lock:
            if self.cam is None:
                self.open()
            r = self.cam.MV_CC_GetFloatValue("ExposureTime", v)
            if r != 0:
                raise RuntimeError(f"读取曝光失败 ret={_err(r)}")
        return {"cur": round(v.fCurValue, 1), "min": round(v.fMin, 1), "max": round(v.fMax, 1)}

    def _get_exposure_locked(self) -> float | None:
        """读当前曝光(µs)，不加锁（open() 内用）。失败返回 None。"""
        v = MVCC_FLOATVALUE()
        memset(byref(v), 0, sizeof(v))
        if self.cam.MV_CC_GetFloatValue("ExposureTime", v) != 0:
            return None
        return round(v.fCurValue, 1)

    def _get_gain_locked(self) -> float | None:
        """读当前增益(dB)，不加锁（open() 内用）。失败返回 None。"""
        v = MVCC_FLOATVALUE()
        memset(byref(v), 0, sizeof(v))
        if self.cam.MV_CC_GetFloatValue("Gain", v) != 0:
            return None
        return round(v.fCurValue, 2)

    def _set_gain_locked(self, db: float):
        """实设增益（不加锁）。调用方须已持 self._lock 或处于 open() 内（cam 已就绪）。"""
        self.cam.MV_CC_SetEnumValueByString("GainAuto", "Off")
        r = self.cam.MV_CC_SetFloatValue("Gain", float(db))
        if r != 0:
            raise RuntimeError(f"设置增益失败 ret={_err(r)}")

    def _apply_boot_params(self):
        """开机曝光/增益策略：优先相机当前值(MVS调的)，丢值(默认~5000µs)才回退持久化备份。

        在 open() 内调用（此刻 cam 已就绪、不加 self._lock，只用 *_locked 系列）。绝不抛错。
        """
        if not self.role:
            return
        param_source = os.environ.get("HIK_PARAM_SOURCE", "backup").strip().lower()
        if param_source == "mvs":
            try:
                cur_exp = self._get_exposure_locked()
            except Exception:  # noqa: BLE001
                cur_exp = None
            try:
                cur_gain = self._get_gain_locked()
            except Exception:  # noqa: BLE001
                cur_gain = None
            param_store.save_role(self.role, exp_us=cur_exp, gain_db=cur_gain)
            log.info("相机 %s 使用 MVS 当前参数：曝光=%s us 增益=%s dB",
                     self.role, cur_exp, cur_gain)
            return
        try:
            cur_exp = self._get_exposure_locked()
        except Exception:  # noqa: BLE001
            cur_exp = None
        if cur_exp is not None and cur_exp > _LOST_EXP_THRESHOLD_US:
            # 相机当前值正常(你在 MVS 调的) → 采用，并把它同步进持久化备份(备份始终=最近好值)
            cur_gain = None
            try:
                cur_gain = self._get_gain_locked()
            except Exception:  # noqa: BLE001
                pass
            param_store.save_role(self.role, exp_us=cur_exp, gain_db=cur_gain)
            log.info("相机 %s 采用当前值 曝光=%.0fµs 增益=%s(已同步备份)", self.role, cur_exp,
                     f"{cur_gain}dB" if cur_gain is not None else "?")
            return
        # 相机丢值(默认/断电回落) → 回退持久化备份
        bak = param_store.load_role(self.role)
        exp, gain = bak.get("exp_us"), bak.get("gain_db")
        log.warning("相机 %s 当前曝光=%s ≤阈值%.0fµs，判定丢值，回退备份 曝光=%s 增益=%s",
                    self.role, cur_exp, _LOST_EXP_THRESHOLD_US, exp, gain)
        if exp is not None:
            try:
                self._set_exposure_locked(float(exp))
            except Exception:  # noqa: BLE001
                pass
        if gain is not None:
            try:
                self._set_gain_locked(float(gain))
            except Exception:  # noqa: BLE001
                pass

    def set_gain(self, db: float):
        """设置增益（dB）。先关自动增益，再设固定值。"""
        with self._lock:
            if self.cam is None:
                self.open()
            self._set_gain_locked(db)
        if self.role:
            param_store.save_role(self.role, gain_db=db)    # 运行时改也更新备份(最近好值)
        return self.get_gain()                                              # 锁外读回（get_gain 自持锁）

    def get_gain(self) -> dict:
        """读当前增益（dB）及可调范围。"""
        v = MVCC_FLOATVALUE()
        memset(byref(v), 0, sizeof(v))
        with self._lock:
            if self.cam is None:
                self.open()
            r = self.cam.MV_CC_GetFloatValue("Gain", v)
            if r != 0:
                raise RuntimeError(f"读取增益失败 ret={_err(r)}")
        return {"cur": round(v.fCurValue, 2), "min": round(v.fMin, 2), "max": round(v.fMax, 2)}

    def snapshot(self, path: str, timeout_ms: int = 3000, quality: int = 90) -> str:
        """抓当前最新一帧，存为 JPG 到 path（ASCII 路径）。纯原生取流：失败直接抛错，不自愈抢相机。"""
        if self.orient != "none":
            # 需方向校正：走 grab_array(已翻正) + cv2 编码存图，保证与预览一致
            import cv2
            arr = self.grab_array(timeout_ms=timeout_ms)
            ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
            if not ok:
                raise RuntimeError("存图失败(imencode)")
            with open(path, "wb") as f:
                f.write(buf.tobytes())
            return path
        lock_wait = float(os.environ.get("HIK_LOCK_WAIT_SEC", "2") or 2)
        if not self._lock.acquire(timeout=lock_wait):
            raise RuntimeError("抓拍锁等待超时（上一次取帧可能卡死在SDK）")
        try:
            if self.cam is None:
                self.open()
            frame = MV_FRAME_OUT()
            memset(byref(frame), 0, sizeof(frame))
            r = self.cam.MV_CC_GetImageBuffer(frame, timeout_ms)
            if r != 0 or not frame.pBufAddr:
                raise RuntimeError(f"取帧失败 ret={_err(r)}（相机在取流但没抓到画面）")
            try:
                fi = frame.stFrameInfo
                st_img = MV_CC_IMAGE()
                memset(byref(st_img), 0, sizeof(MV_CC_IMAGE))
                st_img.nWidth = fi.nExtendWidth or fi.nWidth
                st_img.nHeight = fi.nExtendHeight or fi.nHeight
                st_img.enPixelType = fi.enPixelType
                st_img.pImageBuf = frame.pBufAddr
                st_img.nImageBufSize = fi.nFrameLenEx or fi.nFrameLen
                st_img.nImageLen = st_img.nImageBufSize

                sp = MV_CC_SAVE_IMAGE_PARAM()
                memset(byref(sp), 0, sizeof(MV_CC_SAVE_IMAGE_PARAM))
                sp.enImageType = MV_Image_Jpeg
                sp.iMethodValue = 1                    # 插值方法：最优
                sp.nQuality = int(quality)             # JPG 质量 (50,99]
                sp.nEndian = 0
                r = self.cam.MV_CC_SaveImageToFileEx2(st_img, sp, path)
                if r != 0:
                    raise RuntimeError(f"存图失败 ret={_err(r)}")
            finally:
                self.cam.MV_CC_FreeImageBuffer(frame)
        finally:
            self._lock.release()
        self.last_ok = time.time()
        return path

    def grab_array(self, timeout_ms: int = 3000):
        """抓当前最新一帧并转成 BGR 的 numpy 数组 (H,W,3)。供实时预览/内存识别用。

        注意：这里**不做**自动重连——它被 mjpeg/motion 的连续循环高频调用，若每次超时都
        close+open 会造成重开风暴、拖垮服务。连续流的容错由调用方(mjpeg 累计失败后调 _recover)负责。
        """
        import numpy as np
        # 带超时抢锁：若上一次取帧/抓拍卡死在 SDK 里(线程仍持锁不退)，这里不无限等，
        # 快速抛错让 mjpeg 结束该路流→前端 onerror 重连，避免"200 但 0 帧"整站堵死。
        lock_wait = float(os.environ.get("HIK_LOCK_WAIT_SEC", "2") or 2)
        if not self._lock.acquire(timeout=lock_wait):
            raise RuntimeError("取帧锁等待超时（上一次取帧可能卡死在SDK，正在自愈）")
        try:
            if self.cam is None:
                self.open()
            frame = MV_FRAME_OUT()
            memset(byref(frame), 0, sizeof(frame))
            r = self.cam.MV_CC_GetImageBuffer(frame, timeout_ms)
            if r != 0 or not frame.pBufAddr:
                raise RuntimeError(f"取帧失败 ret={_err(r)}")
            try:
                fi = frame.stFrameInfo
                w, h = fi.nWidth, fi.nHeight
                dst_len = w * h * 3
                dst = (c_ubyte * dst_len)()
                cvt = MV_CC_PIXEL_CONVERT_PARAM()
                memset(byref(cvt), 0, sizeof(cvt))
                cvt.nWidth, cvt.nHeight = w, h
                cvt.enSrcPixelType = fi.enPixelType
                cvt.pSrcData = cast(frame.pBufAddr, POINTER(c_ubyte))
                cvt.nSrcDataLen = fi.nFrameLen
                cvt.enDstPixelType = PixelType_Gvsp_BGR8_Packed
                cvt.pDstBuffer = cast(dst, POINTER(c_ubyte))
                cvt.nDstBufferSize = dst_len
                r = self.cam.MV_CC_ConvertPixelType(cvt)
                if r != 0:
                    raise RuntimeError(f"像素转换失败 ret={_err(r)}")
                arr = np.frombuffer(dst, dtype=np.uint8, count=dst_len).reshape(h, w, 3).copy()
            finally:
                self.cam.MV_CC_FreeImageBuffer(frame)
        finally:
            self._lock.release()
        self.last_ok = time.time()                 # 判活时间戳（看门狗用）
        return _apply_orient(arr, self.orient)     # 锁外做方向校正（纯 CPU）

    def mjpeg(self, preview_w: int | None = None, quality: int = 70, max_fps: int | None = None):
        """实时预览 MJPEG 流生成器：抓帧→缩放→JPEG→multipart，供前端 <img> 直接显示。

        preview_w：预览宽度。None 时读 .env 的 HIK_PREVIEW_W，缺省 0=不缩(原生全尺寸 2448×2048)。
        两块网卡都开了巨型帧(9014)后带宽够，默认走全尺寸；想省带宽把 HIK_PREVIEW_W 设成 960/1280 即可。
        """
        import cv2
        if preview_w is None:                          # 缺省读 .env，0 或空 = 不缩(原生全尺寸)
            preview_w = int(os.environ.get("HIK_PREVIEW_W", "0") or 0)
        if max_fps is None:                            # 预览默认低帧率，省带宽（.env 可调）
            max_fps = int(os.environ.get("HIK_PREVIEW_FPS", "5") or 5)
        period = 1.0 / max(1, max_fps)
        sn = (self.info or {}).get("sn") or self.sn or "?"
        log.info("预览流开始 sn=%s", sn)               # 进流即打点，确认前端确实连上了
        fail = 0
        sent = 0
        try:
            while True:
                try:
                    arr = self.grab_array(timeout_ms=1500)
                    fail = 0
                except Exception as e:  # noqa: BLE001
                    # 纯原生取流：不在流里 close+open 抢相机（那会引发重连风暴、拖垮控制通道）。
                    # 连续取不到帧就结束这路流，前端 <img> onerror 会自动重连、重新开流。
                    fail += 1
                    if fail == 1 or fail % 10 == 0:    # 打真实异常，别再吞掉（这是之前"没画面却无日志"的坑）
                        log.warning("预览取帧失败 sn=%s 第%d次：%s", sn, fail, e)
                    if fail > 20:                      # 连续~3s 取不到帧 → 退出，让前端重连
                        log.warning("预览流退出 sn=%s（连续取帧失败，等前端重连）", sn)
                        break
                    time.sleep(0.15)
                    continue
                if preview_w and arr.shape[1] > preview_w:
                    nh = int(arr.shape[0] * preview_w / arr.shape[1])
                    arr = cv2.resize(arr, (preview_w, nh))
                ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
                if not ok:
                    log.warning("预览 JPEG 编码失败 sn=%s", sn)
                    continue
                jpg = buf.tobytes()
                if sent == 0:                          # 第一帧成功推出 → 画面此刻应已显示
                    log.info("预览首帧已推送 sn=%s 尺寸=%dx%d %d字节", sn, arr.shape[1], arr.shape[0], len(jpg))
                sent += 1
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                time.sleep(period)
        finally:
            log.info("预览流结束 sn=%s 共推 %d 帧", sn, sent)

    def close(self):
        with self._lock:
            if self.cam is not None:
                try:
                    self.cam.MV_CC_StopGrabbing()
                    self.cam.MV_CC_CloseDevice()
                    self.cam.MV_CC_DestroyHandle()
                finally:
                    self.cam = None


# --------------------- 双相机（按角色 front/back 各持一台，按 SN 绑定）---------------------
# 现场绑定：上相机=正面(front)=SN DB1224590；下相机=反面(back)=SN DB1623157。可用 .env 覆盖。
_ROLE_SN = {
    "front": os.environ.get("HIK_FRONT_SN", "DB1224590").strip(),   # 上·正面
    "back":  os.environ.get("HIK_BACK_SN", "DB1623157").strip(),    # 下·反面
}
_CAMERA_MODE = os.environ.get("HIK_CAMERA_MODE", "dual").strip().lower()
_SINGLE_CAMERA = _CAMERA_MODE == "single"
if _CAMERA_MODE in ("single", "uvc"):
    _ROLE_SN["front"] = os.environ.get("HIK_FRONT_SN", "").strip()
    _ROLE_SN["back"] = ""
_cams: dict[str, HikCamera] = {}
_glock = threading.Lock()
# 主动释放相机(给 MVS 调参)时置 True：暂停一切自愈(看门狗/预览流重连)，
# 否则自愈会把"被释放"当成"卡死"抢着重开，和释放/ MVS 打架(0x80000203)。
# 预览/抓拍/静止即拍等"要用相机"的动作会自动解除暂停。
_paused = False


def pause():
    """暂停自愈(释放相机给 MVS 前调用)。"""
    global _paused
    _paused = True


def resume():
    """解除暂停(要用相机时调用)。"""
    global _paused
    _paused = False

# 反面方向校正：反面相机 180° 装，原图上下颠倒且左右镜像。
# **rot180** 既把文字转正(OCR 能读)，又反转左右→正反槽位对齐(identity 映射)——现场实测:
# rot180 解出 11 处日期 / none 9 / fliph 0(镜像成反字读不了) / flipv 0。故默认反面 rot180、正面 none。
# 若换机位不对，用 /api/hik/orient 一键切 none/fliph/flipv/rot180 再实测。
_ORIENT_MODES = ("none", "fliph", "flipv", "rot180")
_ROLE_ORIENT = {
    "front": os.environ.get("HIK_FRONT_ORIENT", "none").strip().lower(),
    "back":  os.environ.get("HIK_BACK_ORIENT", "rot180").strip().lower(),
}
for _r in ("front", "back"):
    if _ROLE_ORIENT[_r] not in _ORIENT_MODES:
        _ROLE_ORIENT[_r] = "none"


def _env_float(key):
    """.env 取 float；空/非法返回 None（表示不设）。"""
    v = os.environ.get(key, "").strip()
    try:
        return float(v) if v != "" else None
    except ValueError:
        return None


# 各角色开机自动应用的曝光/增益（持久化，防相机断电回默认导致过暗/过亮）
_ROLE_EXP = {"front": _env_float("HIK_FRONT_EXPOSURE_US"), "back": _env_float("HIK_BACK_EXPOSURE_US")}
_ROLE_GAIN = {"front": _env_float("HIK_FRONT_GAIN"), "back": _env_float("HIK_BACK_GAIN")}


def _apply_orient(arr, mode):
    """按 mode 翻转/旋转 BGR 数组，让反面与正面在方向上对应（同一根对齐）。"""
    if mode == "none" or mode not in _ORIENT_MODES or arr is None:
        return arr
    import cv2
    if mode == "fliph":
        return cv2.flip(arr, 1)               # 左右镜像（上下对拍反面最常见）
    if mode == "flipv":
        return cv2.flip(arr, 0)               # 上下翻转
    if mode == "rot180":
        return cv2.rotate(arr, cv2.ROTATE_180)
    return arr


def _norm_role(role: str) -> str:
    r = (role or "front").lower()
    return r if r in _ROLE_SN else "front"


_watchdog_on = False
_wd_lock = threading.Lock()


def _watchdog_loop():
    """看门狗：每 5s 巡检已打开的相机；发现卡流/掉线(取帧失败)就自动重连(带退避防死转)。

    - 最近有成功取帧(预览/抓拍在跑)→ 跳过，不打扰；
    - 空闲相机 → 探一帧确认还活着(连续模式一直在出帧，健康则秒回)；
    - 探帧失败 = 卡流/掉线 → _recover()，并退避 15s 再试(相机真拔了也不会狂转)。
    """
    period = float(os.environ.get("HIK_WATCHDOG_SEC", "5") or 5)
    if period <= 0:
        return
    backoff: dict = {}
    while True:
        time.sleep(period)
        if _paused:                                    # 已释放给 MVS：别抢相机
            continue
        for role, cam in list(_cams.items()):
            if cam is None or cam.cam is None:
                continue
            if time.time() - getattr(cam, "opened_ts", 0) < 20:
                continue                                   # 刚开的相机给 20s 热身，别去抢帧
            if time.time() - getattr(cam, "last_ok", 0) < period + 1:
                continue                                   # 刚取过帧，健康，跳过
            try:
                cam.grab_array(timeout_ms=1200)            # 探活（成功会刷新 last_ok）
                backoff.pop(role, None)
            except Exception:                              # 卡流/掉线
                if time.time() >= backoff.get(role, 0):
                    log.warning("看门狗：%s 取帧失败，自动重连", role)
                    try:
                        cam._recover()
                    except Exception:  # noqa: BLE001
                        pass
                    backoff[role] = time.time() + 15       # 退避 15s，防死转


def _ensure_watchdog():
    """看门狗已停用（按现场要求走纯原生 SDK 取流）。

    原来的后台巡检+自愈会在相机打不开/卡流时反复 close+open 抢相机，
    引发重连风暴、长时间占住 _open_lock，把 status/曝光/增益全拖到几十秒、
    预览也出不来。掉线改由前端 <img> onerror 自动重连处理。保留函数名给调用方，空实现。"""
    return


def get_camera(role: str = "front") -> HikCamera:
    """取某角色(front/back)相机实例（按 SN 绑定，首次调用时创建并打开取流）。"""
    role = _norm_role(role)
    resume()                                       # 取相机来用=意图使用→解除释放暂停(仅释放接口会置暂停)
    _ensure_watchdog()
    with _glock:
        cam = _cams.get(role)
        if cam is None:
            sn = _ROLE_SN.get(role) or ""
            orient = _ROLE_ORIENT.get(role, "none")
            exp, gain = _ROLE_EXP.get(role), _ROLE_GAIN.get(role)
            cam = (HikCamera(sn=sn, orient=orient, exp_us=exp, gain_db=gain, role=role) if sn
                   else HikCamera(orient=orient, exp_us=exp, gain_db=gain, role=role))
            _cams[role] = cam
        return cam


def set_orient(role: str, mode: str) -> str:
    """设置某角色相机方向校正（none/fliph/flipv/rot180），即时对预览+抓拍生效。"""
    role = _norm_role(role)
    mode = (mode or "none").strip().lower()
    if mode not in _ORIENT_MODES:
        raise ValueError(f"方向模式必须是 {'/'.join(_ORIENT_MODES)} 之一")
    _ROLE_ORIENT[role] = mode
    cam = _cams.get(role)
    if cam is not None:
        cam.orient = mode                       # 已打开的实例也立刻改
    return mode


def get_orient() -> dict:
    """当前各角色方向校正模式。"""
    return dict(_ROLE_ORIENT)


def snapshot(path: str, role: str = "front", **kw) -> str:
    """抓某角色相机一帧存到 path。"""
    return get_camera(role).snapshot(path, **kw)


def mjpeg(role: str = "front", **kw):
    """某角色相机实时预览 MJPEG 流。"""
    return get_camera(role).mjpeg(**kw)


def set_exposure(us: float, role: str = "front") -> dict:
    return get_camera(role).set_exposure(us)


def get_exposure(role: str = "front") -> dict:
    return get_camera(role).get_exposure()


def set_gain(db: float, role: str = "front"):
    return get_camera(role).set_gain(db)


def get_gain(role: str = "front") -> dict:
    return get_camera(role).get_gain()


def capture_both(front_path: str, back_path: str, **kw) -> dict:
    """**同时**抓 上(正面)/下(反面) 两台各一帧（两线程并行，防正反错位）。任一台失败即抛错。"""
    result: dict = {}

    def _grab(role, p):
        try:
            result[role] = get_camera(role).snapshot(p, **kw)
        except Exception as e:  # noqa: BLE001
            result[role] = e

    ts = [threading.Thread(target=_grab, args=("front", front_path)),
          threading.Thread(target=_grab, args=("back", back_path))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    errs = {r: v for r, v in result.items() if isinstance(v, Exception)}
    if errs:
        raise RuntimeError("；".join(f"{r} 抓拍失败: {v}" for r, v in errs.items()))
    return {"front": front_path, "back": back_path}


def status() -> dict:
    """相机可用性 + 角色→SN 绑定（供前端探测）。不抛错。"""
    if not _SDK_OK:
        return {"sdk": False, "error": _IMPORT_ERR, "devices": [], "roles": _ROLE_SN,
                "camera_mode": _CAMERA_MODE,
                "param_source": os.environ.get("HIK_PARAM_SOURCE", "backup"),
                "orient": dict(_ROLE_ORIENT), "mvimport": _MVIMPORT, "dll_dir": _DLL_DIR}
    try:
        return {"sdk": True, "devices": enum_devices(), "roles": _ROLE_SN,
                "camera_mode": _CAMERA_MODE,
                "param_source": os.environ.get("HIK_PARAM_SOURCE", "backup"),
                "orient": dict(_ROLE_ORIENT), "mvimport": _MVIMPORT, "dll_dir": _DLL_DIR}
    except Exception as e:  # noqa: BLE001
        return {"sdk": True, "error": str(e), "devices": [], "roles": _ROLE_SN,
                "camera_mode": _CAMERA_MODE,
                "param_source": os.environ.get("HIK_PARAM_SOURCE", "backup"),
                "orient": dict(_ROLE_ORIENT), "mvimport": _MVIMPORT, "dll_dir": _DLL_DIR}


def close():
    """关闭所有已打开的相机。"""
    with _glock:
        for cam in _cams.values():
            try:
                cam.close()
            except Exception:
                pass
        _cams.clear()
