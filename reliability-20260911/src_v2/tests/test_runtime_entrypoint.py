import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch

from src_v2.runtime import RuntimeMode
from src_v2.ledger import Ledger
from src_v2.runtime_entrypoint import (
    _assert_live_account_binding,
    _required_account_fingerprint,
    _reconciliation_loop,
    _startup_reconciliation,
    assert_legacy_writers_stopped,
    build_runtime,
    build_verified_live_service,
)


class RuntimeEntrypointTests(unittest.TestCase):
    def _env(self, directory):
        return {
            "V2_LEDGER_PATH": str(Path(directory) / "ledger.sqlite3"),
            "V2_LOCK_PATH": str(Path(directory) / "executor.lock"),
            "V2_TG_CHAT_ID": "-1002069530742",
            "V2_TG_SENDER_ID": "5356537814",
        }

    def test_shadow_is_constructed_without_any_okx_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def forbidden_service_factory():
                calls.append(True)
                raise AssertionError("shadow 不得构建 OKX service")

            runtime = build_runtime(
                mode=RuntimeMode.SHADOW,
                env=self._env(tmp),
                live_service_factory=forbidden_service_factory,
                legacy_writer_guard=lambda: None,
            )

            self.assertEqual(RuntimeMode.SHADOW, runtime.mode)
            self.assertEqual([], calls)

    def test_live_without_exact_ack_is_rejected_before_service_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def service_factory():
                calls.append(True)
                return object()

            with self.assertRaisesRegex(RuntimeError, "明确确认"):
                build_runtime(
                    mode=RuntimeMode.LIVE,
                    env=self._env(tmp),
                    live_service_factory=service_factory,
                    legacy_writer_guard=lambda: None,
                )

            self.assertEqual([], calls)

    def test_live_with_exact_ack_constructs_the_explicit_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            env["V2_LIVE_TRADING_ACK"] = "ENABLE_V2_LIVE_TRADING"
            env["V2_WECHAT_ENABLED"] = "1"
            service = object()

            runtime = build_runtime(
                mode=RuntimeMode.LIVE,
                env=env,
                live_service_factory=lambda: service,
                legacy_writer_guard=lambda: None,
            )

            self.assertEqual(RuntimeMode.LIVE, runtime.mode)
            self.assertIs(service, runtime.service)

    def test_live_without_wechat_notifications_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            env["V2_LIVE_TRADING_ACK"] = "ENABLE_V2_LIVE_TRADING"

            with self.assertRaisesRegex(RuntimeError, "微信通知"):
                build_runtime(
                    mode=RuntimeMode.LIVE,
                    env=env,
                    live_service_factory=lambda: object(),
                    legacy_writer_guard=lambda: None,
                )

    def test_live_startup_does_not_pause_for_notification_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            env["V2_LIVE_TRADING_ACK"] = "ENABLE_V2_LIVE_TRADING"
            env["V2_WECHAT_ENABLED"] = "1"
            ledger = Ledger(env["V2_LEDGER_PATH"])
            notification = ledger.enqueue_notification(
                dedupe_key="old-failure",
                kind="REVIEW_REQUIRED",
                message="旧通知",
            )
            for _ in range(3):
                ledger.mark_notification_failed(notification.notification_id, "timeout")

            runtime = build_runtime(
                mode=RuntimeMode.LIVE,
                env=env,
                live_service_factory=lambda: object(),
                legacy_writer_guard=lambda: None,
            )

            self.assertEqual(RuntimeMode.LIVE, runtime.mode)
            self.assertIsNone(runtime.ledger.automation_pause_reason())

    def test_live_is_rejected_while_any_legacy_writer_is_still_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            env["V2_LIVE_TRADING_ACK"] = "ENABLE_V2_LIVE_TRADING"
            env["V2_WECHAT_ENABLED"] = "1"
            service_calls = []

            def blocked():
                raise RuntimeError("旧交易写入者仍在运行")

            with self.assertRaisesRegex(RuntimeError, "旧交易写入者"):
                build_runtime(
                    mode=RuntimeMode.LIVE,
                    env=env,
                    live_service_factory=lambda: service_calls.append(True),
                    legacy_writer_guard=blocked,
                )

            self.assertEqual([], service_calls)
    def test_live_loop_waits_before_the_next_reconciliation(self):
        calls = []

        class Service:
            def reconcile_open_attempts(self):
                calls.append("reconciled")

        runtime = type(
            "Runtime",
            (), {"mode": RuntimeMode.LIVE, "service": Service()},
        )()

        async def stop_after_first_cycle(_delay):
            raise asyncio.CancelledError

        async def run_cycle():
            with self.assertRaises(asyncio.CancelledError):
                await _reconciliation_loop(
                    runtime,
                    interval=10.0,
                    sleep_fn=stop_after_first_cycle,
                )

        asyncio.run(run_cycle())

        self.assertEqual([], calls)

    def test_live_startup_reconciles_before_listener_can_accept_messages(self):
        calls = []

        class Service:
            def reconcile_open_attempts(self):
                calls.append("startup-reconciled")

        runtime = type(
            "Runtime",
            (), {"mode": RuntimeMode.LIVE, "service": Service()},
        )()

        asyncio.run(_startup_reconciliation(runtime))

        self.assertEqual(["startup-reconciled"], calls)

    def test_live_service_is_returned_only_after_time_and_account_checks(self):
        service = type(
            "Service",
            (), {
                "gateway": type(
                    "Gateway", (), {"market": object(), "account": object()}
                )()
            },
        )()
        checks = []

        result = build_verified_live_service(
            expected_account_fingerprint="a" * 64,
            service_factory=lambda: service,
            time_check=lambda market: checks.append(("time", market)),
            account_check=lambda account: (
                checks.append(("account", account))
                or {"account_fingerprint": "a" * 64}
            ),
        )

        self.assertIs(service, result)
        self.assertEqual(
            [("time", service.gateway.market), ("account", service.gateway.account)],
            checks,
        )

    def test_live_service_rejects_account_identity_mismatch(self):
        service = type(
            "Service",
            (), {
                "gateway": type(
                    "Gateway", (), {"market": object(), "account": object()}
                )()
            },
        )()

        with self.assertRaisesRegex(RuntimeError, "账号身份不匹配"):
            build_verified_live_service(
                expected_account_fingerprint="a" * 64,
                service_factory=lambda: service,
                time_check=lambda _market: None,
                account_check=lambda _account: {
                    "account_fingerprint": "b" * 64
                },
            )

    def test_account_fingerprint_must_be_explicit_full_sha256(self):
        with self.assertRaisesRegex(RuntimeError, "V2_OKX_ACCOUNT_FINGERPRINT"):
            _required_account_fingerprint({})
        with self.assertRaisesRegex(RuntimeError, "格式无效"):
            _required_account_fingerprint(
                {"V2_OKX_ACCOUNT_FINGERPRINT": "abc123"}
            )

        self.assertEqual(
            "a" * 64,
            _required_account_fingerprint(
                {"V2_OKX_ACCOUNT_FINGERPRINT": "A" * 64}
            ),
        )

    def test_live_account_binding_rejects_unbound_or_wrong_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            env["V2_OKX_ACCOUNT_FINGERPRINT"] = "a" * 64

            with self.assertRaisesRegex(RuntimeError, "尚未绑定"):
                _assert_live_account_binding(env)

            ledger = Ledger(env["V2_LEDGER_PATH"])
            ledger.bind_account("b" * 64, reason="test")
            with self.assertRaisesRegex(RuntimeError, "账号不匹配"):
                _assert_live_account_binding(env)

    def test_live_account_binding_accepts_exact_ledger_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            env["V2_OKX_ACCOUNT_FINGERPRINT"] = "a" * 64
            Ledger(env["V2_LEDGER_PATH"]).bind_account(
                "a" * 64, reason="test"
            )

            _assert_live_account_binding(env)

    def test_real_writer_guard_rejects_a_loaded_legacy_launchagent(self):
        result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})
        absent = type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})
        with patch(
            "src_v2.runtime_entrypoint.subprocess.run",
            side_effect=[result, absent, absent, absent],
        ):
            with self.assertRaisesRegex(RuntimeError, "com.bangzhu.bottg"):
                assert_legacy_writers_stopped()

    def test_missing_chat_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            del env["V2_TG_CHAT_ID"]
            with self.assertRaisesRegex(RuntimeError, "V2_TG_CHAT_ID"):
                build_runtime(
                    mode=RuntimeMode.SHADOW,
                    env=env,
                    live_service_factory=lambda: object(),
                    legacy_writer_guard=lambda: None,
                )

    def test_missing_sender_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            del env["V2_TG_SENDER_ID"]
            with self.assertRaisesRegex(RuntimeError, "V2_TG_SENDER_ID"):
                build_runtime(
                    mode=RuntimeMode.SHADOW,
                    env=env,
                    live_service_factory=lambda: object(),
                    legacy_writer_guard=lambda: None,
                )

    def test_empty_chat_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            env["V2_TG_CHAT_ID"] = "   "
            with self.assertRaisesRegex(RuntimeError, "V2_TG_CHAT_ID"):
                build_runtime(
                    mode=RuntimeMode.SHADOW,
                    env=env,
                    live_service_factory=lambda: object(),
                    legacy_writer_guard=lambda: None,
                )

    def test_invalid_chat_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            env["V2_TG_CHAT_ID"] = "not-a-number"
            with self.assertRaisesRegex(RuntimeError, "格式无效"):
                build_runtime(
                    mode=RuntimeMode.SHADOW,
                    env=env,
                    live_service_factory=lambda: object(),
                    legacy_writer_guard=lambda: None,
                )


if __name__ == "__main__":
    unittest.main()
