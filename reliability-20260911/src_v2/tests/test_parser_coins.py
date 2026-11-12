"""新币种识别 + 做多语义扩展 + 条件词门禁 回归测试（阶段：先失败）。"""
import unittest

from src_v2.parser import DecisionKind, analyze_message


def _one_signal(text):
    decision = analyze_message(text)
    assert decision.kind == DecisionKind.READY, (
        f"期望 READY，实际 {decision.kind} {decision.questions}"
    )
    assert len(decision.signals) == 1, f"期望 1 个信号，实际 {len(decision.signals)}"
    return decision.signals[0]


class NewCoinAndLongSemanticsTests(unittest.TestCase):
    # ── 应该 READY ──
    def test_sui_low_absorb_complete(self):
        s = _one_signal("SUI 0.79-0.78区域低吸，止损0.75，止盈0.90")
        self.assertEqual("SUI-USDT-SWAP", s.symbol)
        self.assertEqual("long", s.direction)
        self.assertAlmostEqual(0.785, s.entry_price)
        self.assertAlmostEqual(0.75, s.stop)
        self.assertAlmostEqual(0.90, s.target)

    def test_bch_pullback_buy_complete(self):
        s = _one_signal("BCH 255-250区域回踩买入，止损245，止盈270")
        self.assertEqual("BCH-USDT-SWAP", s.symbol)
        self.assertEqual("long", s.direction)
        self.assertAlmostEqual(252.5, s.entry_price)
        self.assertAlmostEqual(245, s.stop)
        self.assertAlmostEqual(270, s.target)

    def test_zec_complete(self):
        s = _one_signal("ZEC 45-44区域低吸，止损42，止盈50")
        self.assertEqual("ZEC-USDT-SWAP", s.symbol)
        self.assertEqual("long", s.direction)
        self.assertAlmostEqual(44.5, s.entry_price)

    def test_ena_complete(self):
        s = _one_signal("ENA 0.30-0.29区域回踩买入，止损0.27，止盈0.36")
        self.assertEqual("ENA-USDT-SWAP", s.symbol)
        self.assertEqual("long", s.direction)
        self.assertAlmostEqual(0.295, s.entry_price)

    # ── 必须不下单 ──
    def test_sui_conditional_pullback_requires_review(self):
        d = analyze_message(
            "SUI,关注突破后回踩0.79-0.78附近区域，如果撑住，这里是个2.3类买点共振。"
        )
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))
        reasons = "；".join(d.questions)
        self.assertIn("条件", reasons)
        self.assertIn("止损", reasons)

    def test_bch_support_only_requires_review(self):
        d = analyze_message(
            "BCH这种之后也可以关注255-250区域的支撑。这种也是突破后回踩买点。"
        )
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))
        self.assertIn("止损", "；".join(d.questions))

    def test_multi_coin_ambiguity_zero_signals(self):
        d = analyze_message("SUI 和 BCH 这两种结构都可以关注低吸")
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))

    def test_conditional_even_with_full_sl_tp_zero_signals(self):
        d = analyze_message("如果SUI站稳0.79再低吸，止损0.75，止盈0.90")
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))

    def test_unsupported_ticker_not_auto_btc(self):
        d = analyze_message("XYZ 0.5区域低吸，止损0.4，止盈0.7")
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))


class ImplicitBtcFiveDigitStrategyTests(unittest.TestCase):
    def test_complete_five_digit_long_strategy_without_ticker_infers_btc(self):
        d = analyze_message(
            "4小时级别继续在77000-79400附近区间内窄幅震荡。"
            "可靠近78000-77800区域逢低做多，止损77000，止盈79400-80000区域。"
        )
        self.assertEqual(DecisionKind.READY, d.kind)
        self.assertEqual(1, len(d.signals))
        signal = d.signals[0]
        self.assertEqual("BTC-USDT-SWAP", signal.symbol)
        self.assertEqual("long", signal.direction)
        self.assertEqual(77900.0, signal.entry_price)
        self.assertEqual(77000.0, signal.stop)
        self.assertEqual(79400.0, signal.target)

    def test_unknown_ticker_near_five_digit_strategy_is_not_relabelled_btc(self):
        d = analyze_message("XYZ 78000-77800区域逢低做多，止损77000，止盈79400")
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))

    def test_five_digit_strategy_without_ticker_still_rejects_conditions(self):
        d = analyze_message("如果站稳78000后再做多，止损77000，止盈79400")
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))


if __name__ == "__main__":
    unittest.main()
