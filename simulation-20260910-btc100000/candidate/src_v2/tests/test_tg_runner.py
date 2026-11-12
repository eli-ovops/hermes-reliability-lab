"""断线重连 runner 测试 — 用 mock client，不连接真实 Telegram。"""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from src_v2.tg_runner import run_with_reconnect


class ReconnectRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnects_after_transient_failure(self):
        client = Mock()
        client.connect = AsyncMock()
        client.run_until_disconnected = AsyncMock(
            side_effect=[
                ConnectionError("network drop"),
                ConnectionError("network drop"),
                None,  # 第三次正常返回
            ]
        )
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        reconnects = await run_with_reconnect(
            client, sleep_fn=fake_sleep, max_reconnects=5
        )

        self.assertEqual(2, reconnects)
        self.assertEqual(2, client.connect.await_count)
        self.assertEqual([1.0, 2.0], sleeps)  # 指数退避 1s → 2s
        self.assertEqual(3, client.run_until_disconnected.await_count)

    async def test_gives_up_after_max_reconnects(self):
        client = Mock()
        client.connect = AsyncMock()
        client.run_until_disconnected = AsyncMock(
            side_effect=ConnectionError("always down")
        )
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        with self.assertRaises(ConnectionError):
            await run_with_reconnect(
                client, sleep_fn=fake_sleep, max_reconnects=2
            )

        # 1 次正常 + 2 次重连 = 3 次调用，之后抛出
        self.assertEqual(3, client.run_until_disconnected.await_count)
        self.assertEqual([1.0, 2.0], sleeps)

    async def test_clean_disconnect_returns_zero_reconnects(self):
        client = Mock()
        client.connect = AsyncMock()
        client.run_until_disconnected = AsyncMock(return_value=None)

        reconnects = await run_with_reconnect(client)

        self.assertEqual(0, reconnects)


if __name__ == "__main__":
    unittest.main()
