from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


_QUESTION_KEY = re.compile(r"<!-- question-key: ([0-9a-f]{64}) -->")
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class BusinessQuestionCollector:
    """Append accepted user questions to one readable Markdown document.

    The collector deliberately stores only request identifiers and the user
    question. Authentication headers, tool configuration and uploaded-file
    paths never enter this document. Writes are serialized and idempotent for a
    message ID so an HTTP retry does not create a second record.
    """

    def __init__(self, document_path: Path) -> None:
        self.document_path = Path(document_path)
        self._lock = threading.Lock()
        self._known_keys: set[str] | None = None

    def record(
        self,
        *,
        application_id: str,
        conversation_id: str,
        message_id: str,
        question: str,
        recorded_at: datetime | None = None,
    ) -> bool:
        normalized_question = question.replace("\x00", "").replace("\r\n", "\n").strip()
        if not normalized_question:
            return False
        key = self._question_key(application_id, conversation_id, message_id)
        timestamp = (recorded_at or datetime.now(_SHANGHAI_TZ)).astimezone(
            _SHANGHAI_TZ
        )

        with self._lock:
            known_keys = self._load_known_keys()
            if key in known_keys:
                return False
            self.document_path.parent.mkdir(parents=True, exist_ok=True)
            needs_header = (
                not self.document_path.exists()
                or self.document_path.stat().st_size == 0
            )
            with self.document_path.open("a", encoding="utf-8", newline="\n") as stream:
                if needs_header:
                    stream.write(
                        "# 实际业务问题\n\n"
                        "> 本文档由数据智能体自动收集真实用户提问；刷新旧答案不会重复记录。\n\n"
                    )
                stream.write(
                    f"<!-- question-key: {key} -->\n"
                    f"## {timestamp.strftime('%Y-%m-%d %H:%M:%S %z')}\n\n"
                    f"- 应用：{json.dumps(application_id, ensure_ascii=False)}\n"
                    f"- 会话：{json.dumps(conversation_id, ensure_ascii=False)}\n"
                    f"- 消息：{json.dumps(message_id, ensure_ascii=False)}\n"
                    "- 问题：\n\n"
                    + "\n".join(f"> {line}" for line in normalized_question.split("\n"))
                    + "\n\n"
                )
                stream.flush()
            known_keys.add(key)
            return True

    def _load_known_keys(self) -> set[str]:
        if self._known_keys is not None:
            return self._known_keys
        if not self.document_path.is_file():
            self._known_keys = set()
            return self._known_keys
        text = self.document_path.read_text(encoding="utf-8")
        self._known_keys = set(_QUESTION_KEY.findall(text))
        return self._known_keys

    @staticmethod
    def _question_key(
        application_id: str,
        conversation_id: str,
        message_id: str,
    ) -> str:
        payload = json.dumps(
            [application_id, conversation_id, message_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
