"""v2 shadow 探针 — 只读获取数字 chat_id / sender_id。

用途：为 v2 Intake 白名单获取帮主的真实数字 sender_id（旧 bot 只存 username）。
使用独立 session 文件 bangzhu_v2_shadow.session，不与旧 bot_tg 共享 session，
不发送消息、不写 signals.json、不触发交易。

用法：
    .venv/bin/python -m src_v2.shadow_probe

若 session 未登录或过期，会提示需要手机号/验证码，届时暂停让用户操作。
"""
from __future__ import annotations

import asyncio
import os

from src_v2.telegram_config import load_telegram_api_credentials


async def _probe() -> None:
    from telethon import TelegramClient

    api_id, api_hash = load_telegram_api_credentials(os.environ)
    phone = os.environ.get("TG_PHONE", "")
    proxy_host = os.environ.get("TG_PROXY_HOST", "127.0.0.1")
    proxy_port = int(os.environ.get("TG_PROXY_PORT", "7890"))
    target_username = os.environ.get("TG_TARGET_USERNAME", "bangzhu1818")
    session_name = os.environ.get("TG_SHADOW_SESSION", "bangzhu_v2_shadow.session")

    client = TelegramClient(
        session_name,
        api_id,
        api_hash,
        proxy=("socks5", proxy_host, proxy_port),
    )

    await client.connect()
    if not await client.is_user_authorized():
        print("⚠️ 需要登录。请提供手机号和验证码（或扫码）。")
        if not phone:
            raise SystemExit("缺少 TG_PHONE 环境变量；请设置手机号后重试")
        await client.start(phone=phone)
    print("✅ 已登录（shadow session）")

    me = await client.get_me()
    print(f"登录账号: {me.first_name} (@{me.username}) id={me.id}")

    try:
        target = await client.get_entity(target_username)
        print(f"帮主 {target_username}: sender_id={target.id}")
    except Exception as exc:
        print(f"⚠️ 找不到目标用户 {target_username}: {exc}")
        target = None

    print("--- 群列表 ---")
    async for dialog in client.iter_dialogs():
        if dialog.is_group:
            print(f"群 {dialog.name!r}: chat_id={dialog.id}")

    print("--- 结论 ---")
    if target is not None:
        print(
            f"Intake 白名单建议: allowed_sender_ids={{{target.id}}}, "
            f"allowed_chat_ids=<群 chat_id>"
        )
    await client.disconnect()


def main() -> None:
    try:
        asyncio.run(_probe())
    except KeyboardInterrupt:
        print("已取消")


if __name__ == "__main__":
    main()
