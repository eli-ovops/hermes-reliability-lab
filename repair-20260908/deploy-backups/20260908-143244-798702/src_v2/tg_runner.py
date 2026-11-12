"""Telegram 监听 runner — 连接、事件注册、断线重连。

Telethon 延迟导入，本模块的纯逻辑（重连循环）离线可测。
不直接调 OKX，不写 signals.json，凭证全部通过参数注入。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from src_v2.tg_adapter import TelegramAdapter, build_telethon_handlers

log = logging.getLogger("tg_runner")


async def run_with_reconnect(
    client,
    *,
    max_reconnects: int = 5,
    backoff_base: float = 1.0,
    sleep_fn: Callable[[float], Awaitable[None]] | None = None,
) -> int:
    """运行 client 直到显式停止；致命异常时指数退避重连。

    返回发生过的重连次数。超过 max_reconnects 后抛出最后一个异常。
    Telethon 的 run_until_disconnected 内部已处理瞬时网络抖动，
    这里处理的是 FloodWait、鉴权失败等导致循环退出的异常。
    """
    sleep = sleep_fn or asyncio.sleep
    reconnects = 0
    while True:
        try:
            await client.run_until_disconnected()
            return reconnects
        except Exception as exc:  # noqa: BLE001 - 重连循环必须兜底
            reconnects += 1
            if reconnects > max_reconnects:
                raise
            delay = backoff_base * (2 ** (reconnects - 1))
            log.warning(
                "Telegram 断线（%s），%s 秒后第 %s 次重连",
                type(exc).__name__, delay, reconnects,
            )
            await sleep(delay)


async def run_listener(
    adapter: TelegramAdapter,
    *,
    api_id: int,
    api_hash: str,
    session_name: str,
    proxy: tuple | None,
    allowed_chats: set[int],
    max_reconnects: int = 5,
    backoff_base: float = 1.0,
) -> int:
    """构建 Telethon client、注册事件、进入断线重连循环。"""
    from telethon import TelegramClient

    client = TelegramClient(session_name, api_id, api_hash, proxy=proxy)
    build_telethon_handlers(adapter, client, allowed_chats=allowed_chats)
    try:
        await client.start()
    except Exception:
        log.exception("Telegram 初始连接失败")
        raise
    log.info("Telegram 已连接，开始监听")
    return await run_with_reconnect(
        client,
        max_reconnects=max_reconnects,
        backoff_base=backoff_base,
    )
