import unittest
from decimal import Decimal

from src_v2.okx_gateway import OKXGateway


class StubAccount:
    def get_positions(self, **kwargs):
        return {"code": "0", "data": [{"instId": kwargs["instId"], "pos": "0"}]}

    def get_account_balance(self, **kwargs):
        return {"code": "0", "data": [{"details": [{"ccy": "USDT", "availBal": "123"}]}]}


class StubMarket:
    def get_ticker(self, **kwargs):
        return {"code": "0", "data": [{"instId": kwargs["instId"], "last": "65000"}]}


class StubPublic:
    def get_instruments(self, **kwargs):
        return {"code": "0", "data": [{"instId": kwargs["instId"], "tickSz": "0.1"}]}


class StubTrade:
    def amend_algo_order(self, **kwargs):
        self.amend_kwargs = kwargs
        return {
            "code": "0",
            "data": [{"algoId": kwargs["algoId"], "sCode": "0"}],
        }

    def order_algos_history(self, **kwargs):
        self.history_kwargs = kwargs
        return {"code": "0", "data": []}


class GatewayContractTests(unittest.TestCase):
    def test_market_balance_and_zero_positions_are_normalized_without_network(self):
        gateway = OKXGateway(
            account_api=StubAccount(),
            trade_api=StubTrade(),
            market_api=StubMarket(),
            public_api=StubPublic(),
        )

        self.assertEqual(Decimal("65000"), gateway.current_price("BTC-USDT-SWAP"))
        self.assertEqual(Decimal("123"), gateway.available_usdt())
        self.assertEqual([], gateway.positions("BTC-USDT-SWAP"))

    def test_protection_size_amendment_uses_the_official_algo_endpoint(self):
        trade = StubTrade()
        gateway = OKXGateway(
            account_api=StubAccount(),
            trade_api=trade,
            market_api=StubMarket(),
            public_api=StubPublic(),
        )

        response = gateway.amend_protection("BTC-USDT-SWAP", "ALGO-1", "5")

        self.assertEqual("0", response["code"])
        self.assertEqual(
            {
                "instId": "BTC-USDT-SWAP",
                "algoId": "ALGO-1",
                "cxlOnFail": "true",
                "newSz": "5",
            },
            trade.amend_kwargs,
        )

    def test_protection_terminal_state_uses_official_algo_history_endpoint(self):
        trade = StubTrade()
        gateway = OKXGateway(
            account_api=StubAccount(),
            trade_api=trade,
            market_api=StubMarket(),
            public_api=StubPublic(),
        )

        response = gateway.algo_history_response(
            "BTC-USDT-SWAP", algo_id="ALGO-1"
        )

        self.assertEqual("0", response["code"])
        self.assertEqual(
            {
                "ordType": "oco",
                "algoId": "ALGO-1",
                "instId": "BTC-USDT-SWAP",
            },
            trade.history_kwargs,
        )


if __name__ == "__main__":
    unittest.main()
