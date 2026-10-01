"""Platform agent user-prompt store (mirrors the New_Agent platform contract).

The platform keeps user-written prompts in ``data_ask_agent_version``
(role_setting / background / instruction_type).  The New_Agent service reads
the latest version row on every chat request and forwards it as the request
``prompt`` block.  This store reproduces the same behavior on the agent side:

- latest published version wins (``ORDER BY version DESC LIMIT 1``);
- ``instruction_type == 0`` maps role_setting to ``concise_instruct``;
  otherwise role_setting/background map to ``user``/``Aagent_background``;
- every request re-reads the table (bounded short TTL cache only absorbs
  burst traffic), so prompt edits published on the platform apply to the
  next request without redeploying this service.

Failures never block the main chain: an unavailable store simply yields no
user prompt and the built-in system prompts remain authoritative.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 30.0


class AgentPromptStore:
    """Reads the latest platform agent prompt for an application code."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        cache_ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> None:
        self._connection_factory = connection_factory
        self._cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._cache: dict[
            tuple[str, int | None], tuple[float, dict[str, str] | None]
        ] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def from_parameters(
        cls,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        connect_timeout: int = 5,
        read_timeout: int = 10,
        cache_ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> "AgentPromptStore":
        import pymysql

        def connect() -> Any:
            return pymysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                write_timeout=read_timeout,
            )

        return cls(connect, cache_ttl_seconds=cache_ttl_seconds)

    async def resolve(
        self,
        application_id: str,
        *,
        semantic_model_id: int | None = None,
    ) -> dict[str, str] | None:
        """Return the latest prompt dict for the agent code, or None.

        The dict uses the New_Agent field names (``user`` /
        ``Aagent_background`` / ``concise_instruct``) so it can be consumed by
        ``AgentPromptConfig`` unchanged.
        """
        code = (application_id or "").strip()
        if not code:
            return None
        cache_key = (code, semantic_model_id)
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None and now - cached[0] < self._cache_ttl_seconds:
            return cached[1]
        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None and time.monotonic() - cached[0] < self._cache_ttl_seconds:
                return cached[1]
            try:
                prompt = await asyncio.to_thread(
                    self._lookup_latest, code, semantic_model_id
                )
            except Exception as exc:
                logger.warning(
                    "agent prompt lookup failed for %s: %s: %s",
                    code, type(exc).__name__, exc,
                )
                prompt = None
            self._cache[cache_key] = (time.monotonic(), prompt)
            return prompt

    @staticmethod
    def _prompt_from_row(row: dict[str, Any] | None) -> dict[str, str] | None:
        if not row:
            return None
        instruction_type = row.get("instruction_type")
        role_setting = str(row.get("role_setting") or "")
        background = str(row.get("background") or "")
        if instruction_type == 0:
            return {
                "concise_instruct": role_setting,
                "user": "",
                "Aagent_background": "",
            }
        return {
            "user": role_setting,
            "Aagent_background": background,
            "concise_instruct": "",
        }

    def _lookup_latest(
        self, code: str, semantic_model_id: int | None
    ) -> dict[str, str] | None:
        connection = self._connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT instruction_type, role_setting, background "
                    "FROM data_ask_agent_version "
                    "WHERE agent_code = %s "
                    "ORDER BY version DESC LIMIT 1",
                    (code,),
                )
                row = cursor.fetchone()
                if row or semantic_model_id is None:
                    return self._prompt_from_row(row)

                # Some platform routes use a stable service application ID
                # (for example ``data-analysis``) instead of the generated
                # agent_code. Resolve that transport alias only when the
                # request's semantic model belongs to exactly one published
                # active agent; ambiguity deliberately yields no prompt.
                cursor.execute(
                    "SELECT DISTINCT a.id, a.agent_code "
                    "FROM data_ask_agent a "
                    "JOIN data_ask_agent_semantic_model sm ON sm.agent_id = a.id "
                    "WHERE sm.semantic_model_id = %s AND a.status = 2 "
                    "ORDER BY a.publish_time DESC LIMIT 2",
                    (semantic_model_id,),
                )
                agents = cursor.fetchall() or []
                if len(agents) != 1:
                    return None
                cursor.execute(
                    "SELECT instruction_type, role_setting, background "
                    "FROM data_ask_agent_version "
                    "WHERE agent_id = %s AND COALESCE(is_deleted, 0) = 0 "
                    "ORDER BY version DESC LIMIT 1",
                    (agents[0]["id"],),
                )
                return self._prompt_from_row(cursor.fetchone())
        finally:
            connection.close()
