"""统一日志配置模块。

使用方式:
    from logger import logger
    logger.info("信息")
    logger.error("错误")

日志文件:
    ./logs/app.log       (按天滚动，保留 30 天)
    同时输出到控制台
"""
from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# 日志目录
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(filename)s:%(lineno)d | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _build_logger(name: str = "app", level: int = logging.INFO) -> logging.Logger:
    """构建一个带文件滚动 + 控制台输出的 logger"""
    log = logging.getLogger(name)
    log.setLevel(level)

    # 避免重复添加 handler（多次 import 时）
    if log.handlers:
        return log

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # 1. 文件 handler：按天滚动，保留 30 天
    file_handler = TimedRotatingFileHandler(
        filename=LOG_DIR / f"{name}.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    log.addHandler(file_handler)

    # 2. 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    log.addHandler(console_handler)

    # 防止日志向上传递到 root logger 重复输出
    log.propagate = False

    return log


# 全局默认 logger，直接 from logger import logger 使用
logger = _build_logger("app")


if __name__ == "__main__":
    logger.debug("debug 消息")
    logger.info("info 消息")
    logger.warning("warning 消息")
    logger.error("error 消息")
    logger.critical("critical 消息")
