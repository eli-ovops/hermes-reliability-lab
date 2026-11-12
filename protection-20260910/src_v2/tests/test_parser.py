import unittest

from src_v2.parser import DecisionKind, analyze_message


class MessageAnalysisTests(unittest.TestCase):
    def test_support_commentary_requires_review_instead_of_creating_an_order(self):
        decision = analyze_message("BTC 昨日复盘：65000附近支撑，行情仍在震荡")

        self.assertEqual(DecisionKind.NEEDS_REVIEW, decision.kind)
        self.assertEqual((), decision.signals)

    def test_complete_long_strategy_is_ready_with_midpoint_and_nearest_protection(self):
        decision = analyze_message(
            "以太坊靠近1870-1850区域逢低做多，止损1830，止盈1920-1950区域"
        )

        self.assertEqual(DecisionKind.READY, decision.kind)
        self.assertEqual(1, len(decision.signals))
        signal = decision.signals[0]
        self.assertEqual("ETH-USDT-SWAP", signal.symbol)
        self.assertEqual("long", signal.direction)
        self.assertEqual(1860.0, signal.entry_price)
        self.assertEqual(1830.0, signal.stop)
        self.assertEqual(1920.0, signal.target)

    def test_negated_entry_requires_review_even_when_prices_are_complete(self):
        decision = analyze_message(
            "BTC 65000做多，止损64000，止盈67000，但不要做多"
        )

        self.assertEqual(DecisionKind.NEEDS_REVIEW, decision.kind)
        self.assertEqual((), decision.signals)

    def test_complete_part_executes_while_ambiguous_part_is_reported(self):
        decision = analyze_message(
            "BTC 64000做多，止损63000，止盈65000；66000做空，等待确认"
        )

        self.assertEqual(DecisionKind.READY_WITH_REVIEW, decision.kind)
        self.assertEqual(("long",), tuple(signal.direction for signal in decision.signals))
        self.assertEqual(1, len(decision.questions))

    def test_chinese_ten_thousand_price_is_normalized_before_parsing(self):
        decision = analyze_message(
            "BTC 6.8万附近做多，止损67000，止盈70000"
        )

        self.assertEqual(DecisionKind.READY, decision.kind)
        self.assertEqual(68000.0, decision.signals[0].entry_price)

    def test_position_management_message_never_becomes_a_new_entry(self):
        decision = analyze_message("以太坊多单继续持有，止盈上移到1950，减仓一半")

        self.assertEqual(DecisionKind.NEEDS_REVIEW, decision.kind)
        self.assertEqual((), decision.signals)

    def test_duplicate_same_direction_entries_require_review(self):
        decision = analyze_message(
            "BTC 65000做多，止损64000，止盈67000；64500做多，止损63500，止盈66000"
        )

        self.assertEqual(DecisionKind.NEEDS_REVIEW, decision.kind)
        self.assertEqual((), decision.signals)

    def test_complete_dual_direction_strategy_keeps_one_order_per_direction(self):
        decision = analyze_message(
            "BTC 65000做多，止损64000，止盈67000；67000做空，止损68000，止盈65000"
        )

        self.assertEqual(DecisionKind.READY, decision.kind)
        self.assertEqual(
            ("long", "short"), tuple(signal.direction for signal in decision.signals)
        )

    def test_numbered_eth_dual_strategy_does_not_treat_list_numbers_as_prices(self):
        decision = analyze_message(
            "以太坊继续高位震荡上涨，前期日中枢3的上沿在2386-2465区域。"
            "故反弹如进入2380上方则进入较强的阻力区域。日内上方关注2400附近的阻力，"
            "下方关注2300附近的支撑。预期最近一两日或将在此区间内构筑新的30分中枢。"
            "操作思路 1：可靠近2385-2400区域轻仓试空，止损2420，止盈2340-2320。"
            "2：可靠近2300-2280区域逢低做多，止损2260，止盈2350-2370区域。"
        )

        self.assertEqual(DecisionKind.READY, decision.kind)
        self.assertEqual(
            ("short", "long"), tuple(signal.direction for signal in decision.signals)
        )
        short, long = decision.signals
        self.assertEqual((2392.5, 2420.0, 2340.0), (short.entry_price, short.stop, short.target))
        self.assertEqual((2290.0, 2260.0, 2350.0), (long.entry_price, long.stop, long.target))

    def test_soft_negation_cannot_be_misread_as_entry_instruction(self):
        decision = analyze_message(
            "BTC 当前不建议做多，65000做多，止损64000，止盈67000"
        )

        self.assertEqual(DecisionKind.NEEDS_REVIEW, decision.kind)
        self.assertEqual((), decision.signals)

    def test_conditional_breakout_plan_waits_for_user_instead_of_entering_now(self):
        decision = analyze_message(
            "BTC 如果站稳66000再做多，止损65000，止盈68000"
        )

        self.assertEqual(DecisionKind.NEEDS_REVIEW, decision.kind)
        self.assertEqual((), decision.signals)


if __name__ == "__main__":
    unittest.main()
