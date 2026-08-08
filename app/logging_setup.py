"""统一日志配置：分级 + 按天滚动文件 + 控制台 + 错误计数。

`setup_logging()` 幂等，server 启动时调一次。日志写 `logs/app.log`（每天午夜滚动、
保留 14 天：app.log.2026-07-06 …）。一个 `_ErrorCounter` handler 对 WARNING+ 计入
`metrics`，供 /api/metrics 与告警参考。
"""
from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler

from . import metrics
from .core import BASE_DIR

LOG_DIR = os.path.join(BASE_DIR, "logs")
_FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_done = False


class _ErrorCounter(logging.Handler):
    """把 WARNING 及以上级别的日志计入 metrics（错误计数 + 最近错误）。"""

    def emit(self, record):
        try:
            if record.levelno >= logging.WARNING:
                metrics.inc_error(self.format(record))
        except Exception:
            pass


def setup_logging(level: str | int | None = None) -> logging.Logger:
    """初始化根 logger（幂等）。level 缺省取环境变量 LOG_LEVEL 或 INFO。"""
    global _done
    logger = logging.getLogger("yxq")
    if _done:
        return logger
    os.makedirs(LOG_DIR, exist_ok=True)
    lvl = level or os.environ.get("LOG_LEVEL", "INFO")

    root = logging.getLogger()
    root.setLevel(lvl)
    fmt = logging.Formatter(_FMT)

    fh = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"), when="midnight",
        backupCount=int(os.environ.get("LOG_KEEP_DAYS", "14")), encoding="utf-8")
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)

    ec = _ErrorCounter()
    ec.setFormatter(fmt)

    # 避免重复添加
    root.handlers = [h for h in root.handlers
                     if not isinstance(h, (TimedRotatingFileHandler, _ErrorCounter))]
    root.addHandler(fh)
    root.addHandler(ch)
    root.addHandler(ec)

    # uvicorn / fastapi 的日志并入同一套 handler
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # 每请求的访问日志由 server 的中间件统一记（带耗时+降噪），关掉 uvicorn 自带的逐条 access
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _done = True
    logger.info("日志系统就绪 → %s (level=%s)", LOG_DIR, lvl)
    return logger


def get_logger(name: str = "yxq") -> logging.Logger:
    return logging.getLogger(name)
