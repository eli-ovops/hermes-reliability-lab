import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from src_v2.executor import ExecutionStatus
from src_v2.ingest import Intake
from src_v2.ledger import Ledger
from src_v2.runtime import RuntimeMode, RuntimeNotifications, StrategyRuntime
from src_v2.service import TradingService
from src_v2.tg_adapter import TelegramAdapter


CHAT_ID = -1002069530742
SENDER_ID = 5356537814


class RuntimeGateway:
    def __init__(self):
        self.placed = []
        self.orders = {}

    def positions(self, symbol):
        return []

    def pending_orders(self, symbol):
        return []

    def available_usdt(self):
        return Decimal("1000")

    def instrument(self, symbol):
        return {
            "tickSz": "0.1",
            "lotSz": "0.01",
            "minSz": "0.01",
            "ctVal": "0.01",
            "ctMult": "1",
        }

    def current_price(self, symbol):
        return Decimal("65000")

    def ensure_leverage(self, symbol, direction, leverage):
        return {"code": "0", "data": [{"sCode": "0"}]}

    def place_order(self, order):
        self.placed.append(order)
        order_id = f"ORDER-{len(self.placed)}"
        self.orders[order_id] = order
        return {
            "code": "0",
            "data": [{
                "ordId": order_id,
                "clOrdId": order["clOrdId"],
                "sCode": "0",
            }],
        }

    def order(self, symbol, *, ord_id=None, cl_ord_id=None):
        order = self.orders.get(ord_id)
        if order is None:
            return {"code": "51603", "data": []}
        return {
            "code": "0",
            "data": [{
                "instId": symbol,
                "ordId": ord_id,
                "clOrdId": order["clOrdId"],
                "state": "live",
                "accFillSz": "0",
            }],
        }


class StrategyRuntimeTests(unittest.TestCase):
    def _components(self, directory, mode):
        ledger = Ledger(Path(directory) / "ledger.sqlite3")
        adapter = TelegramAdapter(
            intake=Intake(
                ledger=ledger,
                allowed_chat_ids={CHAT_ID},
                allowed_sender_ids={SENDER_ID},
            )
        )
        gateway = RuntimeGateway()
        service = TradingService(
            ledger=ledger,
            gateway=gateway,
            lock_path=Path(directory) / "executor.lock",
            require_source_attestation=mode is RuntimeMode.LIVE,
        )
        runtime = StrategyRuntime(
            adapter=adapter,
            mode=mode,
            service=service if mode is RuntimeMode.LIVE else None,
        )
        return runtime, ledger, gateway

    @staticmethod
    def _message(runtime, message_id=1):
        return runtime.on_new_message(
            chat_id=CHAT_ID,
            sender_id=SENDER_ID,
            sender_username="bangzhu1818",
            message_id=message_id,
            message_date=datetime.now(timezone.utc),
            text="BTC 65000做多，止损64000，止盈67000",
        )

    def test_september_strategy_texts_submit_after_unrelated_deletion(self):
        # Captured source text; all account/order operations use RuntimeGateway.
        cases = [('比特币继续沿5日均线附近窄幅震荡，20日均线上移至78000附近，年线阻力在82000附近。之前在77000-81500附近的震荡中枢，正在从4小时级别升级为日线级别。4小时级别目前处于自82230开启的4小时下跌走势中，30分级别处于自80560开启的30分下跌走势中。 近期上方关注81500-82000区域的阻力，下方关注78000附近的支撑。    操作思路 1：如有回落，靠近20日均线78500-78000区域逢低做多，止损77000，止盈80500-81000区域。   （以上建议有时效性，仅供参考交流）', 'BTC-USDT-SWAP', 78250), ('以太坊继续沿5日均线震荡，20日均线上移。前期在2400-2540附近构筑的4小时中枢即将升级成为日线中枢。目前处于自2536开启的4小时级别调整中。近期上方关注2520-2550区域的阻力，下方关注2450-2420区域的支撑。  操作思路 1：可靠近2450-2430区域逢低做多，止损2400，止盈2520-2550区域。  （以上建议有时效性，仅供参考交流）', 'ETH-USDT-SWAP', 2440)]
        for text, symbol, entry in cases:
            with self.subTest(symbol=symbol), tempfile.TemporaryDirectory() as tmp:
                runtime, ledger, gateway = self._components(tmp, RuntimeMode.LIVE)
                gateway.current_price = lambda symbol: Decimal(str(entry)) + Decimal("1")
                runtime.on_deleted_message(
                    chat_id=CHAT_ID, message_id=67514,
                    message_date=datetime.now(timezone.utc),
                )
                event = dict(
                    chat_id=CHAT_ID, sender_id=SENDER_ID, sender_username="trusted",
                    message_id=67516, message_date=datetime.now(timezone.utc), text=text,
                )
                result = runtime.on_new_message(**event)
                duplicate = runtime.on_new_message(**event)
                self.assertIsNotNone(result.execution)
                self.assertEqual(ExecutionStatus.SUBMITTED, result.execution.status)
                self.assertEqual(1, result.execution.placed)
                self.assertEqual(0, duplicate.execution.placed)
                self.assertEqual(1, len(gateway.placed))
                self.assertEqual(symbol, gateway.placed[0]["instId"])
                self.assertEqual("limit", gateway.placed[0]["ordType"])
                self.assertEqual(Decimal(str(entry)), Decimal(gateway.placed[0]["px"]))
                self.assertEqual("LIVE", ledger.list_attempts(result.strategy_id)[0].state)

    def test_live_message_is_recorded_then_executed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, ledger, gateway = self._components(tmp, RuntimeMode.LIVE)

            first = self._message(runtime)
            duplicate = self._message(runtime)

            self.assertEqual(ExecutionStatus.SUBMITTED, first.execution.status)
            self.assertEqual(1, first.execution.placed)
            self.assertEqual(0, duplicate.execution.placed)
            self.assertEqual(1, len(gateway.placed))
            self.assertEqual("LIVE", ledger.list_attempts(first.strategy_id)[0].state)

    def test_shadow_message_is_recorded_without_constructing_an_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, ledger, gateway = self._components(tmp, RuntimeMode.SHADOW)

            result = self._message(runtime)

            self.assertIsNone(result.execution)
            self.assertIsNotNone(ledger.get_strategy(result.strategy_id))
            self.assertEqual([], gateway.placed)

    def test_live_execution_notification_can_be_delivered_from_the_same_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, ledger, _ = self._components(tmp, RuntimeMode.LIVE)
            self._message(runtime)
            delivered = []

            sent, failed = RuntimeNotifications(runtime).flush_once(
                delivered.append
            )

            self.assertEqual((1, 0), (sent, failed))
            self.assertEqual(1, len(delivered))
            self.assertEqual(0, len(ledger.list_pending_notifications()))

    def test_notification_flush_sends_at_most_one_message_per_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, ledger, _ = self._components(tmp, RuntimeMode.SHADOW)
            ledger.enqueue_notification(
                dedupe_key="notice-1", kind="REVIEW_REQUIRED", message="first"
            )
            ledger.enqueue_notification(
                dedupe_key="notice-2", kind="REVIEW_REQUIRED", message="second"
            )
            delivered = []

            sent, failed = RuntimeNotifications(runtime).flush_once(
                delivered.append
            )

            self.assertEqual((1, 0), (sent, failed))
            self.assertEqual(["first"], delivered)
            self.assertEqual(1, len(ledger.list_pending_notifications()))

    def test_notification_flush_skips_backing_off_head_without_starving_due_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, ledger, _ = self._components(tmp, RuntimeMode.SHADOW)
            first = ledger.enqueue_notification(
                dedupe_key="notice-1", kind="REVIEW_REQUIRED", message="first"
            )
            ledger.enqueue_notification(
                dedupe_key="notice-2", kind="REVIEW_REQUIRED", message="second"
            )
            ledger.mark_notification_failed(first.notification_id, "cooldown")
            delivered = []

            sent, failed = RuntimeNotifications(runtime).flush_once(
                delivered.append
            )

            self.assertEqual((1, 0), (sent, failed))
            self.assertEqual(["second"], delivered)
            pending = ledger.list_pending_notifications()
            self.assertEqual(["first"], [item.message for item in pending])

    def test_notification_flush_can_recover_even_while_automation_is_paused(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, ledger, _ = self._components(tmp, RuntimeMode.SHADOW)
            notification = ledger.enqueue_notification(
                dedupe_key="notice-1", kind="REVIEW_REQUIRED", message="first"
            )
            for _ in range(3):
                ledger.mark_notification_failed(
                    notification.notification_id, "ilink rate limited"
                )
            ledger.pause_automation("微信通知连续失败，等待通道恢复和用户确认")
            with ledger._connection() as connection:
                connection.execute(
                    "UPDATE notifications SET updated_at=? WHERE notification_id=?",
                    ("2000-01-01T00:00:00+00:00", notification.notification_id),
                )
                connection.commit()
            delivered = []

            sent, failed = RuntimeNotifications(runtime).flush_once(
                delivered.append
            )

            self.assertEqual((1, 0), (sent, failed))
            self.assertEqual(["first"], delivered)
            self.assertEqual(0, len(ledger.list_pending_notifications()))


if __name__ == "__main__":
    unittest.main()
