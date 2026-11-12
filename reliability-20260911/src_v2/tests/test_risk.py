import unittest
from decimal import Decimal

from src_v2.parser import TradeSignal
from src_v2.risk import RiskPolicy, RiskReview


class RiskPolicyTests(unittest.TestCase):
    def test_major_coin_limit_order_uses_one_percent_margin_and_fifty_x(self):
        policy = RiskPolicy()
        signal = TradeSignal(
            symbol="BTC-USDT-SWAP",
            direction="long",
            entry_low=65000,
            entry_high=65000,
            entry_price=65000,
            stop=64000,
            target=67000,
        )

        plan = policy.plan(
            signal,
            available_usdt=Decimal("1000"),
            current_price=Decimal("66000"),
            instrument={
                "tickSz": "0.1",
                "lotSz": "0.01",
                "minSz": "0.01",
                "ctVal": "0.01",
                "ctMult": "1",
            },
        )

        self.assertEqual(50, plan.leverage)
        self.assertEqual("limit", plan.order_type)
        self.assertEqual(Decimal("65000"), plan.price)
        self.assertEqual(Decimal("0.76"), plan.size)
        self.assertLessEqual(plan.margin_fraction, Decimal("0.01"))

    def test_chasing_is_rejected_when_current_price_has_already_crossed_the_stop(self):
        policy = RiskPolicy()
        signal = TradeSignal(
            symbol="BTC-USDT-SWAP",
            direction="long",
            entry_low=65000,
            entry_high=65000,
            entry_price=65000,
            stop=64000,
            target=67000,
        )

        with self.assertRaises(RiskReview):
            policy.plan(
                signal,
                available_usdt=Decimal("1000"),
                current_price=Decimal("63800"),
                instrument={
                    "tickSz": "0.1",
                    "lotSz": "1",
                    "minSz": "1",
                    "ctVal": "0.01",
                    "ctMult": "1",
                },
            )

    def test_crossed_entry_within_two_percent_becomes_market_order(self):
        signal = TradeSignal(
            symbol="BTC-USDT-SWAP",
            direction="long",
            entry_low=65000,
            entry_high=65000,
            entry_price=65000,
            stop=63000,
            target=67000,
        )

        plan = RiskPolicy().plan(
            signal,
            available_usdt=Decimal("1000"),
            current_price=Decimal("64500"),
            instrument={
                "tickSz": "0.1",
                "lotSz": "0.01",
                "minSz": "0.01",
                "ctVal": "0.01",
                "ctMult": "1",
            },
        )

        self.assertEqual("market", plan.order_type)
        self.assertEqual(Decimal("64500"), plan.price)

    def test_crossed_entry_over_two_percent_requires_user_review(self):
        signal = TradeSignal(
            symbol="BTC-USDT-SWAP",
            direction="long",
            entry_low=65000,
            entry_high=65000,
            entry_price=65000,
            stop=60000,
            target=68000,
        )

        with self.assertRaises(RiskReview):
            RiskPolicy().plan(
                signal,
                available_usdt=Decimal("1000"),
                current_price=Decimal("63000"),
                instrument={
                    "tickSz": "0.1",
                    "lotSz": "1",
                    "minSz": "1",
                    "ctVal": "0.01",
                    "ctMult": "1",
                },
            )

    def test_alt_coin_uses_ten_x_leverage(self):
        signal = TradeSignal(
            symbol="SOL-USDT-SWAP",
            direction="long",
            entry_low=180,
            entry_high=180,
            entry_price=180,
            stop=170,
            target=200,
        )

        plan = RiskPolicy().plan(
            signal,
            available_usdt=Decimal("1000"),
            current_price=Decimal("185"),
            instrument={
                "tickSz": "0.01",
                "lotSz": "0.01",
                "minSz": "0.01",
                "ctVal": "0.1",
                "ctMult": "1",
            },
        )

        self.assertEqual(10, plan.leverage)
        self.assertLessEqual(plan.margin_fraction, Decimal("0.01"))

    def test_exchange_minimum_never_silently_raises_margin_above_one_percent(self):
        base = TradeSignal(
            symbol="SOL-USDT-SWAP",
            direction="long",
            entry_low=10,
            entry_high=10,
            entry_price=10,
            stop=9,
            target=12,
        )
        common = {
            "tickSz": "0.1",
            "lotSz": "1",
            "ctVal": "1",
            "ctMult": "1",
        }

        with self.assertRaisesRegex(RiskReview, "超过1%"):
            RiskPolicy().plan(
                base,
                available_usdt=Decimal("100"),
                current_price=Decimal("11"),
                instrument={**common, "minSz": "5"},
            )


if __name__ == "__main__":
    unittest.main()
