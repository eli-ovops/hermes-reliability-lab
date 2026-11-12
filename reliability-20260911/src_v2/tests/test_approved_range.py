import unittest
from src_v2.parser import DecisionKind, analyze_message, analyze_approved_range

TEXT = 'HYPE，运行至上涨通道下沿附近，日内关注84-83.5区域能否有效站稳。'

class ApprovedRangeTests(unittest.TestCase):
    def test_explicit_range_has_two_directions_and_two_percent_protection(self):
        d = analyze_approved_range(TEXT, symbol='HYPE-USDT-SWAP', low=83.5, high=84)
        self.assertEqual(DecisionKind.READY, d.kind)
        self.assertEqual(['long', 'short'], [s.direction for s in d.signals])
        for s, prices in zip(d.signals, [(83.5,81.83,85.17),(84,85.68,82.32)]):
            self.assertEqual('HYPE-USDT-SWAP', s.symbol)
            self.assertEqual(prices, (s.entry_price,s.stop,s.target))

    def test_unapproved_watch_message_still_ignored(self):
        self.assertEqual(DecisionKind.IGNORE, analyze_message(TEXT).kind)

    def test_approval_must_match_source_symbol_and_range(self):
        for symbol,low,high in [('BTC-USDT-SWAP',83.5,84),('HYPE-USDT-SWAP',80,84),('HYPE-USDT-SWAP',84,83.5)]:
            with self.subTest(symbol=symbol,low=low), self.assertRaises(ValueError):
                analyze_approved_range(TEXT,symbol=symbol,low=low,high=high)
