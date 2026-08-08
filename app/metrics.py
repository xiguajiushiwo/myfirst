"""内存级运行指标：累计处理数、良率、错误数、运行时长。

供 `GET /api/metrics` 展示、以及看板/告警参考。进程重启即清零（不落库，落库统计另见 storage）。
线程安全。db.save_record 每存一条记录调用 record_verdict；日志系统对 ERROR 调 inc_error。
"""
from __future__ import annotations

import datetime
import threading

_lock = threading.Lock()
_c = {"records_total": 0, "pass": 0, "fail": 0, "unknown": 0, "errors": 0}
_vl = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
_started = datetime.datetime.now()
_last_error = ""


def add_vl_usage(usage: dict | None) -> None:
    """累计一次多模态大模型调用的用量（usage 来自 OpenAI 兼容响应，可能为空）。"""
    with _lock:
        _vl["calls"] += 1
        if usage:
            _vl["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            _vl["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            _vl["total_tokens"] += int(usage.get("total_tokens", 0) or 0)


def vl_usage() -> dict:
    with _lock:
        return dict(_vl)


def vl_usage_delta(before: dict | None, after: dict | None = None) -> dict:
    """返回两次累计快照之间的用量，供一次检测单独展示和入库。"""
    start = before or {}
    end = after or vl_usage()
    return {
        key: max(0, int(end.get(key, 0) or 0) - int(start.get(key, 0) or 0))
        for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
    }


def record_verdict(verdict: str) -> None:
    """每保存一条质检记录调用一次，按综合判定累计。"""
    key = verdict if verdict in ("pass", "fail") else "unknown"
    with _lock:
        _c["records_total"] += 1
        _c[key] += 1


def inc_error(msg: str = "") -> None:
    global _last_error
    with _lock:
        _c["errors"] += 1
        if msg:
            _last_error = msg[:200]


def snapshot() -> dict:
    with _lock:
        d = dict(_c)
        last = _last_error
    total = d["records_total"]
    d["yield"] = round(d["pass"] / total, 4) if total else None
    d["uptime_sec"] = int((datetime.datetime.now() - _started).total_seconds())
    d["started_at"] = _started.strftime("%Y-%m-%d %H:%M:%S")
    d["last_error"] = last
    return d
