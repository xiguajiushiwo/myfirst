"""相机曝光/增益**持久化备份**：存每台相机"最近一次的好值"，供开机时判定相机丢值后回退。

策略（配合 hik_sdk.open()）：
- 开机读相机**当前**曝光(你在 MVS 调的)。若正常(> 丢失阈值) → 采用它并**同步写进本文件**(备份=最近好值)。
- 若判定为默认/丢失(海康默认曝光 ~5000µs，断电会回落) → 回退用本文件里的备份刷回硬件。
- 运行时经工作台/接口改曝光增益时也更新本文件。

首次无文件 → 用 .env 的 HIK_{FRONT,BACK}_{EXPOSURE_US,GAIN} 播种。文件：config/camera_params.json。
"""
from __future__ import annotations

import json
import os
import threading

from ..core import BASE_DIR

_PATH = os.path.join(BASE_DIR, "config", "camera_params.json")
_lock = threading.Lock()


def _env_float(key: str):
    v = os.environ.get(key, "").strip()
    try:
        return float(v) if v != "" else None
    except ValueError:
        return None


def _seed() -> dict:
    """无备份文件时用 .env 的角色曝光/增益播种。"""
    return {
        "front": {"exp_us": _env_float("HIK_FRONT_EXPOSURE_US"), "gain_db": _env_float("HIK_FRONT_GAIN"),
                  "orient": os.environ.get("HIK_FRONT_ORIENT", "none").strip().lower()},
        "back": {"exp_us": _env_float("HIK_BACK_EXPOSURE_US"), "gain_db": _env_float("HIK_BACK_GAIN"),
                 "orient": os.environ.get("HIK_BACK_ORIENT", "rot180").strip().lower()},
    }


def _read() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def load_role(role: str) -> dict:
    """取某角色的备份好值 {exp_us, gain_db}。无文件/无该角色 → 回退 .env 播种值。"""
    with _lock:
        data = _read()
        if role in data and isinstance(data[role], dict):
            return dict(data[role])
        return _seed().get(role, {})


def save_role(role: str, exp_us: float | None = None, gain_db: float | None = None,
              orient: str | None = None) -> None:
    """更新某角色备份好值（只更非 None 的项）。原子写，避免半截文件。"""
    if not role:
        return
    with _lock:
        data = _read() or _seed()
        cur = dict(data.get(role) or {})
        if exp_us is not None:
            cur["exp_us"] = round(float(exp_us), 1)
        if gain_db is not None:
            cur["gain_db"] = round(float(gain_db), 2)
        if orient is not None:
            cur["orient"] = str(orient)
        data[role] = cur
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PATH)
