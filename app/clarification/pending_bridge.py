"""挂起（pending）状态读写转发。

供接线层在判定为 CLARIFY 时落盘反问、在下轮判定前读取旧挂起。挂起
候选的确认与恢复语义保持不变，这里只做参数转发。
"""
from __future__ import annotations

from typing import Any


async def load_pending(sessions: Any, identity: Any, chat: Any) -> Any | None:
    """读取当前会话的活动挂起，无挂起返回 None。"""
    return await sessions.get_pending(
        identity.tenant_id,
        identity.user_id,
        chat.application_id,
        chat.conversation_id,
    )


async def discard_pending(sessions: Any, identity: Any, chat: Any) -> bool:
    """作废当前挂起：判定为 NEW_TASK 或合并问题将重新执行时调用。"""
    return await sessions.clear_pending(
        identity.tenant_id,
        identity.user_id,
        chat.application_id,
        chat.conversation_id,
    )
