"""多模态大模型（VL）服务配置：多 provider + 当前启用，JSON 持久化。

设计成"服务商列表 + active"，为将来接入**多个**多模态模型预留位置：
只要是 OpenAI 兼容的 /chat/completions 接口（DashScope 通义千问、智谱 GLM-4V、
本地 vLLM、OpenAI 等），加一条 provider（base_url/model/api_key/timeout）即可切换。

首次无配置文件时，从 .env（QWEN_*/DASHSCOPE_*）播种出第一个 provider。
"""
from __future__ import annotations

import json
import os
import threading

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
_PATH = os.path.join(_DIR, "vl_providers.json")
_lock = threading.Lock()
_cache: dict | None = None


def _seed_from_env() -> dict:
    return {
        "active": "dashscope",
        "providers": [{
            "id": "dashscope",
            "name": "通义千问 Qwen-VL（阿里云 DashScope）",
            "base_url": os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "model": os.environ.get("QWEN_VL_MODEL", "qwen3-vl-235b-a22b-instruct"),
            "api_key": os.environ.get("DASHSCOPE_API_KEY", "").strip(),
            "timeout": int(os.environ.get("QWEN_TIMEOUT", "120")),
            "price_per_1k": 0.0,   # 每 1k token 单价(元)，用于费用估算，默认0=不算
        }],
    }


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if os.path.isfile(_PATH):
            try:
                with open(_PATH, encoding="utf-8") as f:
                    _cache = json.load(f)
            except Exception:
                _cache = _seed_from_env()
        else:
            _cache = _seed_from_env()
            _save(_cache)
    return _cache


def _save(cfg: dict) -> None:
    global _cache
    os.makedirs(_DIR, exist_ok=True)
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    _cache = cfg


def active_provider() -> dict:
    """当前启用的 provider（供 quality_inspect 取 base_url/model/key/timeout）。"""
    cfg = _load()
    pid = cfg.get("active")
    for p in cfg.get("providers", []):
        if p.get("id") == pid:
            return p
    return (cfg.get("providers") or [{}])[0]


def ordered_providers() -> list[dict]:
    """降级顺序：当前启用的排第一，其余按原顺序跟随。

    用于大模型调用容错——第一个失败(网络/欠费/超时/报错)就自动试下一个。
    """
    cfg = _load()
    pid = cfg.get("active")
    provs = cfg.get("providers", [])
    active = [p for p in provs if p.get("id") == pid]
    rest = [p for p in provs if p.get("id") != pid]
    return active + rest


def _mask(k: str) -> str:
    k = k or ""
    if len(k) <= 8:
        return "****" if k else ""
    return k[:4] + "****" + k[-4:]


def get_config(masked: bool = True) -> dict:
    """返回配置；masked=True 时 api_key 打码（给前端展示用）。"""
    cfg = _load()
    out = {"active": cfg.get("active"), "providers": []}
    for p in cfg.get("providers", []):
        q = dict(p)
        q["has_key"] = bool(p.get("api_key"))
        if masked:
            q["api_key"] = _mask(p.get("api_key", ""))
        out["providers"].append(q)
    return out


def upsert_provider(p: dict) -> dict:
    """新增或更新一个 provider（按 id）。api_key 为空串则**保留原值**（不覆盖）。"""
    cfg = _load()
    pid = (p.get("id") or "").strip() or (p.get("name") or "vl").strip().lower().replace(" ", "-")[:24]
    p["id"] = pid
    provs = cfg.setdefault("providers", [])
    for i, ex in enumerate(provs):
        if ex.get("id") == pid:
            if not p.get("api_key"):          # 不传 key = 保留原 key
                p["api_key"] = ex.get("api_key", "")
            provs[i] = {**ex, **p}
            _save(cfg)
            return provs[i]
    provs.append(p)
    _save(cfg)
    return p


def set_active(pid: str) -> bool:
    cfg = _load()
    if any(x.get("id") == pid for x in cfg.get("providers", [])):
        cfg["active"] = pid
        _save(cfg)
        return True
    return False


def delete_provider(pid: str) -> bool:
    cfg = _load()
    provs = cfg.get("providers", [])
    n = [x for x in provs if x.get("id") != pid]
    if len(n) == len(provs) or not n:        # 不存在 或 不允许删到空
        return False
    cfg["providers"] = n
    if cfg.get("active") == pid:
        cfg["active"] = n[0]["id"]
    _save(cfg)
    return True
