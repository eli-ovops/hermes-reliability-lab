import unittest

from src_v2.parser import DecisionKind, analyze_message


class CenterRangeStrategyTests(unittest.TestCase):
    def assert_dual_range(self, text, symbol, low, high):
        decision = analyze_message(text)

        self.assertEqual(DecisionKind.READY, decision.kind, decision.questions)
        self.assertEqual(2, len(decision.signals))
        by_direction = {signal.direction: signal for signal in decision.signals}
        self.assertEqual({"long", "short"}, set(by_direction))

        long_signal = by_direction["long"]
        self.assertEqual(symbol, long_signal.symbol)
        self.assertAlmostEqual(low, long_signal.entry_price)
        self.assertAlmostEqual(low * 0.98, long_signal.stop)
        self.assertAlmostEqual(low * 1.02, long_signal.target)

        short_signal = by_direction["short"]
        self.assertEqual(symbol, short_signal.symbol)
        self.assertAlmostEqual(high, short_signal.entry_price)
        self.assertAlmostEqual(high * 1.02, short_signal.stop)
        self.assertAlmostEqual(high * 0.98, short_signal.target)

    def test_aave_primary_range_ignores_later_support_subrange(self):
        self.assert_dual_range(
            "AAVE，在121-130附近区间震荡。关注下沿122-120区域的支撑。",
            "AAVE-USDT-SWAP",
            121.0,
            130.0,
        )

    def test_hype_range_is_dual_direction(self):
        self.assert_dual_range(
            "HYPE，79-83附近区间震荡。",
            "HYPE-USDT-SWAP",
            79.0,
            83.0,
        )

    def test_link_range_is_supported(self):
        self.assert_dual_range(
            "LINK，11-12附近的震荡区间",
            "LINK-USDT-SWAP",
            11.0,
            12.0,
        )

    def test_link_range_inside_oscillation_is_dual_direction(self):
        self.assert_dual_range(
            "LINK，11.0-12.06区间内震荡。",
            "LINK-USDT-SWAP",
            11.0,
            12.06,
        )

    def test_arb_center_range_is_supported(self):
        self.assert_dual_range(
            "ARB，遇阻回落，日内预期在0.13-0.141附近构筑新的30分中枢。",
            "ARB-USDT-SWAP",
            0.13,
            0.141,
        )

    def test_center_word_with_timeframe_is_dual_direction(self):
        self.assert_dual_range(
            "HYPE，在76.7-82.5附近构筑4小时中枢。",
            "HYPE-USDT-SWAP",
            76.7,
            82.5,
        )

    def test_range_without_unique_symbol_never_defaults_to_btc(self):
        decision = analyze_message("79-83附近区间震荡")

        self.assertEqual(DecisionKind.NEEDS_REVIEW, decision.kind)
        self.assertEqual(0, len(decision.signals))
        self.assertIn("币种", "；".join(decision.questions))

    def test_conditional_range_still_requires_review(self):
        decision = analyze_message("如果HYPE回到79-83区间再进")

        self.assertEqual(DecisionKind.NEEDS_REVIEW, decision.kind)
        self.assertEqual(0, len(decision.signals))


if __name__ == "__main__":
    unittest.main()
