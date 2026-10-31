"""logger —— SnapOCR 滚动文件日志。

日志位置：启动文件同目录 app.log（无 data/logs 层）；按 1MB 滚动，
保留 2 个备份，级别 INFO。
设计：全局共享同一个文件 handler（保证不重复写文件），但每个模块用自己名字
的 logger（logging.getLogger(__name__)），日志里能看到来源模块。
线程安全：logging 自带锁，跨线程（托盘/热键/OCR 工作线程）均可安全写入。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import get_app_dir

_LOG_MAX_BYTES = 1_000_000   # 1MB 触发滚动
_LOG_BACKUP_COUNT = 2
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

_handler: RotatingFileHandler | None = None


def _shared_handler() -> RotatingFileHandler:
    """惰性创建全局共享文件 handler（单例）。"""
    global _handler
    if _handler is None:
        get_app_dir().mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            get_app_dir() / "app.log",
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_FORMAT))
        _handler = handler
    return _handler


def get_logger(name: str = "snap_ocr") -> logging.Logger:
    """获取按名字命名的 logger（共享同一文件 handler）。"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 避免重复输出到根 logger
    handler = _shared_handler()
    if handler not in logger.handlers:
        logger.addHandler(handler)
    return logger
