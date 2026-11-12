"""微信发送器接线测试 — 用 mock subprocess，不真实发送。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src_v2.ledger import Ledger
from src_v2.notifications import NotificationDispatcher
from src_v2.wechat_sender import WeChatSender


class WeChatSenderTests(unittest.TestCase):
    def test_send_success_returns_normally(self):
        sender = WeChatSender()
        with patch(
            "src_v2.wechat_sender.subprocess.run",
            return_value=type("R", (), {"returncode": 0, "stdout": '{"success":true}', "stderr": ""})(),
        ) as run:
            sender.send("测试")
        cmd = run.call_args[0][0]
        self.assertIn("weixin", cmd)
        self.assertIn("测试", cmd)

    def test_zero_exit_skipped_is_not_delivery(self):
        with patch("src_v2.wechat_sender.subprocess.run",
                   return_value=type("R", (), {"returncode":0,"stdout":'{"skipped":true}',"stderr":""})()):
            with self.assertRaises(RuntimeError): WeChatSender().send("测试")

    def test_send_failure_raises(self):
        sender = WeChatSender()
        with patch(
            "src_v2.wechat_sender.subprocess.run",
            return_value=type("R", (), {"returncode": 1, "stdout": "", "stderr": "down"})(),
        ):
            with self.assertRaises(RuntimeError):
                sender.send("测试")

    def test_dispatcher_integrates_wechat_sender_and_backoff(self):
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            ledger.enqueue_notification(
                dedupe_key="wechat:1",
                kind="EXECUTION",
                message="策略已提交",
            )
            dispatcher = NotificationDispatcher(ledger=ledger)

            def failing_sender(_):
                raise RuntimeError("wechat down")

            t0 = datetime.now(timezone.utc)
            sent, failed = dispatcher.flush(
                failing_sender, now=t0, backoff_base=60.0
            )
            self.assertEqual((0, 1), (sent, failed))
            # 失败保留 PENDING 供重试
            self.assertEqual(1, len(ledger.list_pending_notifications()))


if __name__ == "__main__":
    unittest.main()
