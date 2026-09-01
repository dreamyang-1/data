from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class DatabaseLoadError(ValueError):
    """Raised when a caller-provided database identifier is unusable."""


@dataclass(frozen=True, slots=True)
class DatabaseSelection:
    """A request-scoped selection of a platform-registered data source.

    The identifier is opaque to the agent. Credentials, hosts and database
    names remain owned by SQL Translator/Redis and are never accepted from the
    model or exposed in prompts.
    """

    database_id: int

    @property
    def data_source_id(self) -> str:
        return str(self.database_id)

    def as_tool_output(self) -> dict[str, Any]:
        return {
            "success": True,
            "tool": "load_database",
            "database_id": self.database_id,
            "data_source_id": self.data_source_id,
            "scope": "request",
        }


class DatabaseLoadTool:
    """Load one backend-selected database for the current analysis request.

    Loading means binding a registered ``database_id`` to the SQL execution
    request. It deliberately does not open arbitrary connection strings and it
    does not persist a global selection between users or conversations.
    """

    name = "load_database"
    description = (
        "加载后端为当前数据分析请求选择的平台数据库。仅接收已注册数据库ID，"
        "不接收主机、账号、密码或模型生成的连接信息。"
    )

    def __call__(self, database_id: int) -> dict[str, Any]:
        return self.load(database_id).as_tool_output()

    @staticmethod
    def load(database_id: int) -> DatabaseSelection:
        if isinstance(database_id, bool) or not isinstance(database_id, int):
            raise DatabaseLoadError("database_id must be a positive integer")
        if database_id <= 0:
            raise DatabaseLoadError("database_id must be a positive integer")
        # Keep the value compatible with signed BIGINT identifiers used by the
        # platform and reject unbounded Python integers at the trust boundary.
        if database_id > 9_223_372_036_854_775_807:
            raise DatabaseLoadError("database_id exceeds the supported range")
        return DatabaseSelection(database_id=database_id)


def build_database_load_tool() -> DatabaseLoadTool:
    """Build the stateless request-scoped database loading tool."""

    return DatabaseLoadTool()
