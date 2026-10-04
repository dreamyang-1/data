from __future__ import annotations

from datetime import datetime

import pytest

from app.services.upload_file_resolver import (
    PlatformUploadFileResolver,
    UploadReferenceResolutionError,
)


class FakeCursor:
    def __init__(self, records, files=()):
        self.records = list(records)
        self.files = list(files)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, parameters):
        self.calls.append((sql, parameters))

    def fetchone(self):
        return self.records.pop(0)

    def fetchall(self):
        return self.files


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_regular_object_name_bypasses_platform_lookup():
    resolver = PlatformUploadFileResolver(
        lambda: pytest.fail("regular object names must not query platform metadata")
    )

    result = await resolver.resolve(
        ["uploads/source.xlsx"],
        conversation_id="conversation-a",
        question="analyze it",
    )

    assert result == ["uploads/source.xlsx"]


@pytest.mark.asyncio
async def test_no_parse_resolves_exact_question_inside_current_conversation():
    cursor = FakeCursor(
        records=[{"chat_session_record_id": "record-a"}],
        files=[
            {"filename": "folder\\source.xlsx"},
            {"filename": "folder\\source.xlsx"},
        ],
    )
    connection = FakeConnection(cursor)
    resolver = PlatformUploadFileResolver(lambda: connection)

    result = await resolver.resolve(
        ["no-parse"],
        conversation_id="conversation-a",
        question="analyze it",
    )

    assert result == ["folder/source.xlsx"]
    assert connection.closed is True
    assert len(cursor.calls) == 2
    first_sql, first_parameters = cursor.calls[0]
    assert "r.chat_session_id = %s" in first_sql
    assert "TRIM(r.chat_value) = TRIM(%s)" in first_sql
    assert "r.create_time >= %s" not in first_sql
    assert first_parameters == ("conversation-a", "analyze it", "no-parse")
    assert cursor.calls[1][1] == ("record-a", "no-parse")


@pytest.mark.asyncio
async def test_no_parse_uses_only_recent_same_conversation_fallback():
    cursor = FakeCursor(
        records=[None, {"chat_session_record_id": "record-b"}],
        files=[{"filename": "source.xlsx"}],
    )
    resolver = PlatformUploadFileResolver(lambda: FakeConnection(cursor))

    result = await resolver.resolve(
        ["no-parse"],
        conversation_id="conversation-b",
        question="normalized differently",
    )

    assert result == ["source.xlsx"]
    fallback_sql, fallback_parameters = cursor.calls[1]
    assert "r.chat_session_id = %s" in fallback_sql
    assert "r.create_time >= %s" in fallback_sql
    assert "TRIM(r.chat_value) = TRIM(%s)" not in fallback_sql
    assert fallback_parameters[0] == "conversation-b"
    assert isinstance(fallback_parameters[1], datetime)
    assert fallback_parameters[2] == "no-parse"


@pytest.mark.asyncio
async def test_no_parse_rejects_missing_or_unsafe_platform_filename():
    cursor = FakeCursor(
        records=[{"chat_session_record_id": "record-c"}],
        files=[{"filename": "../another-conversation.xlsx"}],
    )
    resolver = PlatformUploadFileResolver(lambda: FakeConnection(cursor))

    with pytest.raises(UploadReferenceResolutionError, match="MinIO"):
        await resolver.resolve(
            ["no-parse"],
            conversation_id="conversation-c",
            question="analyze it",
        )
