"""Resolve platform upload markers to authorized MinIO object names."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any


NO_PARSE_MARKER = "no-parse"


class UploadReferenceResolutionError(RuntimeError):
    """The current upload marker could not be bound to one platform file row."""


class PlatformUploadFileResolver:
    """Read the current conversation's persisted upload metadata.

    The platform upload control uses ``no-parse`` to mean that the original
    binary should not be converted into temporary text.  It is not a MinIO
    object name.  The file record saved for the same user turn contains the
    real object name in ``filename``.  Conversation ids are guaranteed by the
    platform contract to be globally unique, so this lookup never searches by
    recency or filename across conversations.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        max_age_seconds: int = 600,
    ) -> None:
        self._connection_factory = connection_factory
        self._max_age_seconds = max(30, min(int(max_age_seconds), 3600))

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
        max_age_seconds: int = 600,
    ) -> "PlatformUploadFileResolver":
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

        return cls(connect, max_age_seconds=max_age_seconds)

    async def resolve(
        self,
        references: list[str],
        *,
        conversation_id: str,
        question: str,
    ) -> list[str]:
        """Replace current-turn ``no-parse`` markers with persisted filenames."""

        if NO_PARSE_MARKER not in {item.casefold() for item in references}:
            return list(references)
        filenames = await asyncio.to_thread(
            self._lookup_current_uploads,
            conversation_id,
            question,
        )
        if not filenames:
            raise UploadReferenceResolutionError(
                "未找到本轮上传文件对应的MinIO对象名"
            )
        resolved: list[str] = []
        inserted = False
        for reference in references:
            if reference.casefold() != NO_PARSE_MARKER:
                resolved.append(reference)
                continue
            if not inserted:
                resolved.extend(filenames)
                inserted = True
        if not resolved or len(resolved) > 10:
            raise UploadReferenceResolutionError("本轮上传文件数量无效")
        return list(dict.fromkeys(resolved))

    def _lookup_current_uploads(
        self,
        conversation_id: str,
        question: str,
    ) -> list[str]:
        threshold = datetime.now() - timedelta(seconds=self._max_age_seconds)
        connection = self._connection_factory()
        try:
            with connection.cursor() as cursor:
                record_id = self._find_record_id(
                    cursor,
                    conversation_id=conversation_id,
                    question=question,
                    threshold=None,
                    require_question_match=True,
                )
                if record_id is None:
                    # Some platform clients normalize surrounding whitespace
                    # after persisting the user record.  The fallback remains
                    # scoped to the same globally unique conversation and a
                    # short current-upload window.
                    record_id = self._find_record_id(
                        cursor,
                        conversation_id=conversation_id,
                        question=question,
                        threshold=threshold,
                        require_question_match=False,
                    )
                if record_id is None:
                    return []
                cursor.execute(
                    """
                    SELECT filename
                    FROM project_chat_session_file_record
                    WHERE chat_session_record_id = %s
                      AND COALESCE(is_deleted, 0) = 0
                      AND LOWER(TRIM(temp_file_path)) = %s
                    ORDER BY sort ASC, id ASC
                    """,
                    (record_id, NO_PARSE_MARKER),
                )
                rows = cursor.fetchall()
        finally:
            connection.close()
        filenames: list[str] = []
        for row in rows:
            value = row.get("filename") if isinstance(row, dict) else row[0]
            normalized = self._valid_object_name(value)
            if normalized is not None:
                filenames.append(normalized)
        return list(dict.fromkeys(filenames))

    @staticmethod
    def _find_record_id(
        cursor: Any,
        *,
        conversation_id: str,
        question: str,
        threshold: datetime | None,
        require_question_match: bool,
    ) -> str | None:
        question_clause = "AND TRIM(r.chat_value) = TRIM(%s)" if require_question_match else ""
        time_clause = "AND r.create_time >= %s" if threshold is not None else ""
        parameters: list[Any] = [conversation_id]
        if threshold is not None:
            parameters.append(threshold)
        if require_question_match:
            parameters.append(question)
        parameters.append(NO_PARSE_MARKER)
        cursor.execute(
            f"""
            SELECT r.chat_session_record_id
            FROM project_chat_session_record r
            JOIN project_chat_session_file_record f
              ON f.chat_session_record_id = r.chat_session_record_id
             AND COALESCE(f.is_deleted, 0) = 0
            WHERE r.chat_session_id = %s
              AND r.role = 'user'
              AND COALESCE(r.is_deleted, 0) = 0
              {time_clause}
              {question_clause}
              AND LOWER(TRIM(f.temp_file_path)) = %s
            ORDER BY r.create_time DESC, f.id DESC
            LIMIT 1
            """,
            tuple(parameters),
        )
        row = cursor.fetchone()
        if not row:
            return None
        value = row.get("chat_session_record_id") if isinstance(row, dict) else row[0]
        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def _valid_object_name(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().replace("\\", "/")
        if (
            not normalized
            or len(normalized) > 1024
            or normalized.startswith("/")
            or ".." in normalized.split("/")
            or normalized.casefold() == NO_PARSE_MARKER
        ):
            return None
        return normalized
