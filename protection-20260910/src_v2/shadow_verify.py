"""交叉验证：从帮主群拉最近消息，确认 sender_id 与 username 的映射真实一致。"""
from __future__ import annotations

import asyncio
import os

from src_v2.telegram_config import load_telegram_api_credentials


async def _verify() -> None:
    from telethon import TelegramClient

    api_id, api_hash = load_telegram_api_credentials(os.environ)
    session_name = os.environ.get("TG_SHADOW_SESSION", "bangzhu_v2_shadow.session")
    chat_id = int(os.environ.get("TG_CHAT_ID", "-1002069530742"))
    expected_sender = int(os.environ.get("TG_SENDER_ID", "5356537814"))

    client = TelegramClient(session_name, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("未登录")

    entity = await client.get_entity(chat_id)
    print(f"群实体: {getattr(entity, 'title', entity)} chat_id={chat_id}")

    matched = 0
    checked = 0
    async for message in client.iter_messages(entity, limit=20):
        sender = message.sender
        if sender is None:
            continue
        sender_id = getattr(sender, "id", None)
        username = getattr(sender, "username", None)
        checked += 1
        if sender_id == expected_sender and username == "bangzhu1818":
            matched += 1
        print(
            f"  msg_id={message.id} sender_id={sender_id} "
            f"username={username} text={str(message.text)[:40]!r}"
        )

    print(f"--- 交叉验证结果 ---")
    print(f"检查消息 {checked} 条，其中 sender_id={expected_sender} 且 username=bangzhu1818 的 {matched} 条")
    if matched > 0:
        print("✅ sender_id=5356537814 与 username=bangzhu1818 映射一致，通过交叉验证")
    else:
        print("❌ 未在最近消息中找到该 sender_id，需人工复核")
    await client.disconnect()


def main() -> None:
    asyncio.run(_verify())


if __name__ == "__main__":
    main()
