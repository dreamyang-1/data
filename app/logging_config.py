"""集中日志配置：一次 setup，让全项目已有的 logger 调用真正输出。

目标：某个节点出问题时，可以按 ``trace_id``（= chat.message_id）在
journald/文件里 grep 出该请求全链路的节点日志。格式统一为::

    时间 级别 模块 [trace_id] 消息

trace_id 通过 ContextVar 注入（api.py 在请求入口 set/finally reset），
orchestrator、adapters 等模块的现有 logger 调用零改动自动携带。

敏感信息约定：SQL 文本、数据行、请求头凭证不进日志；业务摘要入日志前
由调用方先过 EventPayloadSanitizer 并截断。
"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

# 请求级 trace_id（= chat.message_id）。由 app/api.py 在请求入口写入。
# asyncio 任务会拷贝 ContextVar，DAG 子任务日志同样携带。
_log_trace_id: ContextVar[str | None] = ContextVar(
    "data_agent_log_trace_id", default=None
)


def set_log_trace_id(trace_id: str) -> object:
    """绑定当前异步上下文的 trace_id，返回 token 供 reset_log_trace_id 使用。"""
    return _log_trace_id.set(trace_id or None)


def reset_log_trace_id(token: object) -> None:
    """恢复 trace_id 上下文（请求结束/生成器收尾时调用）。"""
    if token is not None:
        _log_trace_id.reset(token)  # type: ignore[arg-type]


class TraceIdFilter(logging.Filter):
    """把 ContextVar 中的 trace_id 注入每条 LogRecord，空值占位为 ``-``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        trace_id = _log_trace_id.get()
        record.trace_id = trace_id if trace_id else "-"
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(trace_id)s] %(message)s"

_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# 三方库噪声收敛：uvicorn 自带 handler 会与集中配置重复输出。
_QUIET_LOGGERS = {
    "uvicorn.access": ("WARNING", False),
    "uvicorn.error": ("WARNING", False),
    "httpx": ("WARNING", True),
    "httpcore": ("WARNING", True),
    "pymysql": ("WARNING", True),
}

_configured = False


def _build_handlers(formatter: logging.Formatter) -> list[logging.Handler]:
    trace_filter = TraceIdFilter()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(trace_filter)
    return [console]


def setup_logging(log_level: str, log_file: str = "",
                  log_max_bytes: int = 10 * 1024 * 1024,
                  log_backup_count: int = 5) -> None:
    """初始化 root logger。幂等：重复调用只会刷新级别，不叠加 handler。

    log_file 为空时只写 stdout（systemd/journald 抓取）；非空时额外启用
    按大小轮转的文件输出（log_backup_count=0 表示不保留历史轮转文件）。
    """
    global _configured
    requested = (log_level or "INFO").upper()
    level = requested if requested in _VALID_LEVELS else "INFO"
    root = logging.getLogger()

    if _configured:
        # 仅允许运行中调整级别，不再重复挂 handler。
        root.setLevel(level)
        logging.getLogger("app").setLevel(level)
        return
    _configured = True

    formatter = logging.Formatter(LOG_FORMAT)
    handlers: list[logging.Handler] = _build_handlers(formatter)

    if log_file:
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            log_file, maxBytes=max(int(log_max_bytes), 1024),
            backupCount=int(log_backup_count), encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(TraceIdFilter())
        handlers.append(file_handler)

    for handler in handlers:
        root.addHandler(handler)

    for name, (quiet_level, propagate) in _QUIET_LOGGERS.items():
        quiet = logging.getLogger(name)
        quiet.setLevel(quiet_level)
        quiet.propagate = propagate

    root.setLevel(level)
    _guard_app_namespace(level)


def _guard_app_namespace(level: int) -> None:
    """守住 ``app`` logger 命名空间，保证 app.* 日志能到达 root。

    部署机会把 Oagnet 的 ``logger.py`` 懒加载进同一进程：它默认用 ``"app"``
    这个名字构建 logger，挂自己的管道格式 handler 并关闭 propagate，导致
    app.services.orchestrator 等子 logger 的日志在 "app" 层被截走，root 上
    的 trace_id 配置失效。这里清掉残留 handler、放一个 NullHandler 哨兵并
    保持 propagate：Oagnet 的 ``if log.handlers: return`` 守卫会直接早退，
    不再重复劫持；日志继续向上交给 root 统一格式化输出。
    """
    app_logger = logging.getLogger("app")
    for handler in list(app_logger.handlers):
        app_logger.removeHandler(handler)
    if not app_logger.handlers:
        app_logger.addHandler(logging.NullHandler())
    app_logger.propagate = True
    app_logger.setLevel(level)
