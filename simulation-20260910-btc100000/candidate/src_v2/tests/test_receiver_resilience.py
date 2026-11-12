import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from src_v2.tg_adapter import build_telethon_handlers
from src_v2.tg_runner import run_listener

class ReceiverResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_sender_event_does_not_require_username_network_lookup(self):
        adapter=Mock()
        client=Mock()
        client.on=lambda builder: lambda handler:handler
        on_new, _, _=build_telethon_handlers(adapter,client,allowed_chats={-100})
        event=SimpleNamespace(chat_id=-100,sender_id=1818,sender=None,
            get_sender=AsyncMock(side_effect=ConnectionError('sender lookup offline')),
            message=SimpleNamespace(id=1,date=datetime.now(timezone.utc),text='BTC 65000做多，止损64000，止盈67000'))
        await on_new(event)
        adapter.on_new_message.assert_called_once()
        event.get_sender.assert_not_awaited()

    async def test_listener_enables_catchup_and_closes_on_cancellation(self):
        client=Mock()
        client.on=lambda builder:lambda handler:handler
        client.start=AsyncMock()
        client.run_until_disconnected=AsyncMock(side_effect=asyncio.CancelledError())
        client.disconnect=AsyncMock()
        with patch('telethon.TelegramClient',return_value=client) as factory:
            with self.assertRaises(asyncio.CancelledError):
                await run_listener(Mock(),api_id=1,api_hash='offline-fixture',session_name='unused',proxy=None,allowed_chats={-100})
        self.assertTrue(factory.call_args.kwargs['catch_up'])
        client.disconnect.assert_awaited_once()

    async def test_background_read_error_does_not_terminate_worker_loop(self):
        from src_v2.runtime_entrypoint import _reconciliation_loop
        from src_v2.runtime import RuntimeMode
        service=Mock()
        service.reconcile_open_attempts.side_effect=[ConnectionError('offline'),None]
        runtime=SimpleNamespace(mode=RuntimeMode.LIVE,service=service)
        cycles=0
        async def sleep(_):
            nonlocal cycles
            cycles+=1
            if cycles==3: raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await _reconciliation_loop(runtime,sleep_fn=sleep)
        self.assertEqual(2,service.reconcile_open_attempts.call_count)
        service.dispatch_pending.assert_called_once()
