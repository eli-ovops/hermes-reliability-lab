import unittest
from src_v2.parser import analyze_message

class CompactBTCEntryTests(unittest.TestCase):
    def test_exact_user_example(self):
        d=analyze_message('btc 100000空单。105000止损，98000止盈')
        self.assertEqual('READY',d.kind.value)
        self.assertEqual(1,len(d.signals))
        s=d.signals[0]
        self.assertEqual(('BTC-USDT-SWAP','short',100000,105000,98000),(s.symbol,s.direction,s.entry_price,s.stop,s.target))

    def test_position_management_or_conditions_cannot_become_new_entries(self):
        base='btc 100000空单。105000止损，98000止盈'
        for text in ['持有'+base,'已有'+base,'取消'+base,'如果到了'+base,base+'，继续持有',base+'，不要下单']:
            with self.subTest(text=text):
                self.assertNotEqual('READY',analyze_message(text).kind.value)

    def test_wrong_side_stop_still_rejected(self):
        self.assertNotEqual('READY',analyze_message('btc 100000空单。95000止损，98000止盈').kind.value)
