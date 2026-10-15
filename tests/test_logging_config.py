"""集中日志配置（app/logging_config）的行为回归。

覆盖：trace_id 注入与空值占位、setup_logging 幂等、格式含 trace_id、
log_file 为空时不产生文件 handler。
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from app.logging_config import (
    LOG_FORMAT,
    TraceIdFilter,
    reset_log_trace_id,
    set_log_trace_id,
    setup_logging,
)


@pytest.fixture()
def isolated_root_logger(monkeypatch):
    """隔离 root logger：清空 pytest 自带 handler 并重置模块级已配置标志。"""
    from app import logging_config as logging_config_module

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers[:] = []
    monkeypatch.setattr(logging_config_module, "_configured", False)
    yield root
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            handler.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def test_trace_id_filter_fills_placeholder_when_unset():
    record = logging.LogRecord(
        "app.test", logging.INFO, __file__, 1, "message", None, None
    )
    assert TraceIdFilter().filter(record) is True
    assert record.trace_id == "-"


def test_set_and_reset_log_trace_id_roundtrip():
    token = set_log_trace_id("msg-123")
    try:
        record = logging.LogRecord(
            "app.test", logging.INFO, __file__, 1, "message", None, None
        )
        TraceIdFilter().filter(record)
        assert record.trace_id == "msg-123"
    finally:
        reset_log_trace_id(token)
    record = logging.LogRecord(
        "app.test", logging.INFO, __file__, 1, "message", None, None
    )
    TraceIdFilter().filter(record)
    assert record.trace_id == "-"


def test_set_log_trace_id_with_empty_value_uses_placeholder():
    token = set_log_trace_id("")
    try:
        record = logging.LogRecord(
            "app.test", logging.INFO, __file__, 1, "message", None, None
        )
        TraceIdFilter().filter(record)
        assert record.trace_id == "-"
    finally:
        reset_log_trace_id(token)


def _own_handlers(root: logging.Logger) -> list[logging.Handler]:
    """本模块挂的 handler：pytest 运行期会临时挂自己的 handler，按格式识别过滤。"""
    return [
        handler
        for handler in root.handlers
        if isinstance(handler.formatter, logging.Formatter)
        and handler.formatter._fmt == LOG_FORMAT
    ]


def test_setup_logging_attaches_single_handler(isolated_root_logger):
    setup_logging("INFO", "")
    assert len(_own_handlers(isolated_root_logger)) == 1


def test_setup_logging_is_idempotent(isolated_root_logger):
    setup_logging("INFO", "")
    setup_logging("INFO", "")
    assert len(_own_handlers(isolated_root_logger)) == 1


def test_setup_logging_is_idempotent_across_repeated_calls(isolated_root_logger):
    for _ in range(3):
        setup_logging("INFO", "")
    assert len(_own_handlers(isolated_root_logger)) == 1


def test_setup_logging_no_file_handler_when_log_file_empty(isolated_root_logger):
    setup_logging("INFO", "")
    assert not any(
        isinstance(handler, logging.FileHandler)
        for handler in _own_handlers(isolated_root_logger)
    )


def test_setup_logging_creates_file_handler_when_configured(
    isolated_root_logger, tmp_path
):
    log_file = tmp_path / "agent.log"
    setup_logging("INFO", str(log_file))
    file_handlers = [
        handler
        for handler in _own_handlers(isolated_root_logger)
        if isinstance(handler, logging.FileHandler)
    ]
    assert len(file_handlers) == 1


def test_log_format_contains_trace_id_placeholder():
    assert "%(trace_id)s" in LOG_FORMAT


def test_info_log_reaches_stdout_with_trace_id(isolated_root_logger, capsys):
    setup_logging("INFO", "")
    token = set_log_trace_id("msg-trace-1")
    try:
        logging.getLogger("app.services.test").info("节点日志样例")
    finally:
        reset_log_trace_id(token)
    output = capsys.readouterr().out
    assert "节点日志样例" in output
    assert "[msg-trace-1]" in output
    assert "app.services.test" in output


def test_setup_logging_rejects_unknown_level_with_info_fallback(
    isolated_root_logger,
):
    setup_logging("NOT_A_LEVEL", "")
    assert isolated_root_logger.level == logging.INFO


def test_uvicorn_loggers_quieted(isolated_root_logger):
    setup_logging("INFO", "")
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
    assert logging.getLogger("uvicorn.access").propagate is False
    assert logging.getLogger("httpx").level == logging.WARNING


def test_settings_expose_log_fields():
    from app.config import Settings

    settings = Settings(env="test")
    assert settings.log_level == "INFO"
    assert settings.log_file == ""
    assert settings.log_max_bytes > 0
    assert settings.log_backup_count >= 0


def test_main_create_app_initializes_logging():
    """create_app 必须接线 setup_logging，保证容器日志生效。"""
    import inspect

    from app import main

    source = inspect.getsource(main.create_app)
    assert "setup_logging(" in source


def test_setup_logging_clears_hijacked_app_logger(isolated_root_logger):
    """Oagnet logger.py 以 "app" 为名挂 handler 并关闭 propagate，必须被纠正。"""
    hijack = logging.StreamHandler()
    app_logger = logging.getLogger("app")
    saved_handlers = list(app_logger.handlers)
    saved_propagate = app_logger.propagate
    saved_level = app_logger.level
    app_logger.addHandler(hijack)
    app_logger.propagate = False
    try:
        setup_logging("INFO", "")
        assert hijack not in app_logger.handlers
        assert app_logger.propagate is True
    finally:
        for handler in list(app_logger.handlers):
            if handler not in saved_handlers:
                app_logger.removeHandler(handler)
        app_logger.handlers[:] = saved_handlers
        app_logger.propagate = saved_propagate
        app_logger.setLevel(saved_level)


def test_app_logger_sentinel_blocks_rehijack_and_keeps_propagation(
    isolated_root_logger, capsys
):
    """setup_logging 后再模拟 Oagnet _build_logger：守卫早退，日志仍带 trace_id 到 root。"""
    setup_logging("INFO", "")
    app_logger = logging.getLogger("app")
    assert app_logger.propagate is True
    # 模拟 Oagnet logger.py 的守卫逻辑：已有 handler 则直接返回。
    if app_logger.handlers:
        pass
    else:  # pragma: no cover - setup_logging 必然已放置哨兵
        app_logger.addHandler(logging.NullHandler())
        app_logger.propagate = False
    token = set_log_trace_id("msg-guard-1")
    try:
        logging.getLogger("app.services.orchestrator").info("守护回归样例")
    finally:
        reset_log_trace_id(token)
    output = capsys.readouterr().out
    assert "[msg-guard-1]" in output
    assert "守护回归样例" in output
