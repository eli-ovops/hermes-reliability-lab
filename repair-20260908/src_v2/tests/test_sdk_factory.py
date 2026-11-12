"""OKX SDK factory 测试 — 阶段三。

用 fake SDK 验证：demo flag、账户双向持仓模式校验、服务器时间偏差校验。
不连接真实 OKX。
"""
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src_v2.okx_gateway import GatewayError


class SDKFactoryTests(unittest.TestCase):
    def test_build_gateway_passes_demo_flag(self):
        captured = {}

        fake_account_mod = types.ModuleType("okx.Account")
        fake_trade_mod = types.ModuleType("okx.Trade")
        fake_market_mod = types.ModuleType("okx.MarketData")
        fake_public_mod = types.ModuleType("okx.PublicData")

        def make_api(name):
            def factory(*args, **kwargs):
                captured[name] = kwargs
                return MagicMock()
            return factory

        fake_account_mod.AccountAPI = make_api("account")
        fake_trade_mod.TradeAPI = make_api("trade")
        fake_market_mod.MarketAPI = make_api("market")
        fake_public_mod.PublicAPI = make_api("public")

        okx_pkg = types.ModuleType("okx")
        okx_pkg.Account = fake_account_mod
        okx_pkg.Trade = fake_trade_mod
        okx_pkg.MarketData = fake_market_mod
        okx_pkg.PublicData = fake_public_mod
        for mod in (fake_account_mod, fake_trade_mod, fake_market_mod, fake_public_mod):
            sys.modules[mod.__name__] = mod
        sys.modules["okx"] = okx_pkg
        sys.modules["okx.Account"] = fake_account_mod
        sys.modules["okx.Trade"] = fake_trade_mod
        sys.modules["okx.MarketData"] = fake_market_mod
        sys.modules["okx.PublicData"] = fake_public_mod

        try:
            from src_v2.sdk_factory import build_gateway

            build_gateway(
                api_key="k", api_secret="s", passphrase="p", demo=True
            )

            self.assertEqual("1", captured["account"]["flag"])  # demo flag
            self.assertEqual("1", captured["trade"]["flag"])
            self.assertEqual("1", captured["market"]["flag"])
            self.assertEqual("1", captured["public"]["flag"])
        finally:
            for name in ("okx", "okx.Account", "okx.Trade", "okx.MarketData", "okx.PublicData"):
                sys.modules.pop(name, None)

    def test_verify_account_config_rejects_single_position_mode(self):
        from src_v2.sdk_factory import verify_account_config

        account_api = MagicMock()
        account_api.get_account_config.return_value = {
            "code": "0",
            "data": [{
                "posMode": "net_mode",
                "acctLv": "1",
                "mainUid": "main-1",
                "uid": "uid-1",
            }],
        }
        with self.assertRaises(GatewayError):
            verify_account_config(account_api)

    def test_verify_account_config_accepts_dual_position_mode(self):
        from src_v2.sdk_factory import verify_account_config

        account_api = MagicMock()
        account_api.get_account_config.return_value = {
            "code": "0",
            "data": [{
                "posMode": "long_short_mode",
                "acctLv": "1",
                "mainUid": "main-1",
                "uid": "uid-1",
            }],
        }
        result = verify_account_config(account_api)
        self.assertEqual("long_short_mode", result["pos_mode"])
        self.assertEqual(
            "00cd1333799f090a970cb681ade1741a34340d3f762e133d33e8952c938d7cde",
            result["account_fingerprint"],
        )

    def test_verify_account_config_rejects_missing_account_identity(self):
        from src_v2.sdk_factory import verify_account_config

        account_api = MagicMock()
        account_api.get_account_config.return_value = {
            "code": "0",
            "data": [{"posMode": "long_short_mode", "acctLv": "1"}],
        }

        with self.assertRaisesRegex(GatewayError, "账户身份"):
            verify_account_config(account_api)

    def test_server_time_skew_within_tolerance(self):
        from src_v2.sdk_factory import server_time_skew_seconds

        now = datetime.now(timezone.utc)
        market_api = MagicMock()
        market_api._get_timestamp.return_value = (
            now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        skew = server_time_skew_seconds(market_api)
        self.assertLess(abs(skew), 5.0)

    def test_server_time_skew_beyond_tolerance_raises(self):
        from src_v2.sdk_factory import server_time_skew_seconds

        now = datetime.now(timezone.utc)
        market_api = MagicMock()
        market_api._get_timestamp.return_value = (
            (now - timedelta(seconds=120)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        with self.assertRaises(GatewayError):
            server_time_skew_seconds(market_api)


if __name__ == "__main__":
    unittest.main()
