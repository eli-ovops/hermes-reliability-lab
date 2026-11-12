import unittest
from decimal import Decimal
from src_v2.parser import analyze_message, DecisionKind, TradeSignal
from src_v2.risk import RiskPolicy, RiskReview

class ProtectionSemanticsTests(unittest.TestCase):
    def test_target_range_cannot_invent_a_missing_stop(self):
        d=analyze_message('BTC 65000做多，止盈64000-67000')
        self.assertEqual(DecisionKind.NEEDS_REVIEW,d.kind)
        self.assertEqual((),d.signals)

    def test_reversed_labels_are_not_silently_swapped(self):
        d=analyze_message('BTC 65000做多，止损67000，止盈64000')
        self.assertEqual(DecisionKind.NEEDS_REVIEW,d.kind)
        self.assertEqual((),d.signals)

    def test_tick_rounding_cannot_collapse_protection_onto_entry(self):
        signal=TradeSignal('HYPE-USDT-SWAP','long',84,84,84,83.99,84.01)
        with self.assertRaises(RiskReview):
            RiskPolicy().plan(signal,available_usdt=Decimal('1000'),current_price=Decimal('85'),instrument={'tickSz':'0.1','lotSz':'1','minSz':'1','ctVal':'1'})
