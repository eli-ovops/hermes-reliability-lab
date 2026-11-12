"""Telegram 适配层 — 把 Telethon 事件转成 SourceEnvelope 交给 Intake。

只做数据转换，不做用户名认证，不直接调 OKX，不写 signals.json。
数字 chat_id 和 sender_id 是唯一认证依据，由 Intake 白名单强制执行。
Telethon 依赖在 runner 层延迟导入，本模块离线可测。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from src_v2.ingest import IngestResult, Intake, SourceEnvelope


class TelegramAdapter:
    """Telegram 事件 → SourceEnvelope → Intake 的纯数据适配器。"""

    def __init__(self, *, intake: Intake):
        self.intake = intake

    def on_new_message(
        self,
        *,
        chat_id: int,
        sender_id: int,
        sender_username: str,
        message_id: int,
        message_date: datetime,
        text: str,
    ) -> IngestResult:
        return self.intake.ingest(
            SourceEnvelope(
                chat_id=chat_id,
                sender_id=sender_id,
                sender_username=sender_username,
                message_id=message_id,
                message_date=message_date,
                text=text,
                event_kind="new",
            )
        )

    def on_edited_message(
        self,
        *,
        chat_id: int,
        sender_id: int,
        sender_username: str,
        message_id: int,
        message_date: datetime,
        text: str,
    ) -> IngestResult:
        return self.intake.ingest(
            SourceEnvelope(
                chat_id=chat_id,
                sender_id=sender_id,
                sender_username=sender_username,
                message_id=message_id,
                message_date=message_date,
                text=text,
                event_kind="edited",
            )
        )

    def on_deleted_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        message_date: datetime,
    ) -> IngestResult:
        # Telegram 删除事件不提供发送者 ID；删除不产生新订单，只触发人工确认。
        return self.intake.ingest(
            SourceEnvelope(
                chat_id=chat_id,
                sender_id=0,
                sender_username="",
                message_id=message_id,
                message_date=message_date,
                text="",
                event_kind="deleted",
            )
        )


def build_telethon_handlers(
    adapter: TelegramAdapter,
    client,
    *,
    allowed_chats: set[int] | frozenset[int],
):
    """在给定的 Telethon client 上注册 New/Edited/Deleted 事件处理器。

    sender_id 从事件直接取数字 ID，绝不使用 username 认证。
    返回三个 handler 协程，便于测试时直接调用。
    """
    from telethon import events
    event_lock = asyncio.Lock()

    @client.on(events.NewMessage(chats=tuple(allowed_chats)))
    async def on_new(event):
        sender_id = getattr(event, "sender_id", None)
        if sender_id is None:
            sender = await event.get_sender()
            sender_id = getattr(sender, "id", 0)
        message = event.message
        if message is None or not (message.text or "").strip():
            return
        async with event_lock:
            return await asyncio.to_thread(
                adapter.on_new_message,
                chat_id=event.chat_id,
                sender_id=int(sender_id or 0),
                sender_username=getattr(
                    await event.get_sender(), "username", ""
                ),
                message_id=message.id,
                message_date=message.date,
                text=message.text,
            )

    @client.on(events.MessageEdited(chats=tuple(allowed_chats)))
    async def on_edited(event):
        sender_id = getattr(event, "sender_id", None)
        if sender_id is None:
            sender = await event.get_sender()
            sender_id = getattr(sender, "id", 0)
        message = event.message
        if message is None or not (message.text or "").strip():
            return
        async with event_lock:
            return await asyncio.to_thread(
                adapter.on_edited_message,
                chat_id=event.chat_id,
                sender_id=int(sender_id or 0),
                sender_username=getattr(
                    await event.get_sender(), "username", ""
                ),
                message_id=message.id,
                message_date=message.date,
                text=message.text,
            )

    @client.on(events.MessageDeleted(chats=tuple(allowed_chats)))
    async def on_deleted(event):
        deleted_ids = getattr(event, "deleted_ids", None)
        if not deleted_ids:
            return
        async with event_lock:
            for message_id in deleted_ids:
                await asyncio.to_thread(
                    adapter.on_deleted_message,
                    chat_id=event.chat_id,
                    message_id=message_id,
                    message_date=datetime.now(),
                )

    return on_new, on_edited, on_deleted
