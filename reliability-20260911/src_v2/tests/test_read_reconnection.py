import unittest
from unittest.mock import Mock

import httpx

from src_v2 import sdk_factory


class ReadReconnectionTests(unittest.TestCase):
    def test_transport_failure_replaces_client_and_retries_only_the_read(self):
        broken, healthy = Mock(), Mock()
        broken.get_positions.side_effect = httpx.ConnectTimeout('private diagnostic')
        healthy.get_positions.return_value = {'code':'0', 'data':[]}
        factory = Mock(side_effect=[broken, healthy])
        api = sdk_factory.ReconnectingAPI(factory, read_methods={'get_positions'})
        self.assertEqual({'code':'0', 'data':[]}, api.get_positions(instId='BTC-USDT-SWAP'))
        self.assertEqual(2, factory.call_count)
        broken.close.assert_called_once()
        healthy.get_positions.assert_called_once_with(instId='BTC-USDT-SWAP')

    def test_write_timeout_is_never_replayed_and_next_read_gets_new_client(self):
        broken, healthy = Mock(), Mock()
        broken.place_order.side_effect = httpx.ReadTimeout('unknown acceptance')
        factory = Mock(side_effect=[broken, healthy])
        api = sdk_factory.ReconnectingAPI(factory, read_methods={'get_order'})
        with self.assertRaises(httpx.ReadTimeout):
            api.place_order(clOrdId='same-id')
        broken.place_order.assert_called_once()
        healthy.place_order.assert_not_called()
        api.get_order(clOrdId='same-id')
        healthy.get_order.assert_called_once()
        healthy.place_order.assert_not_called()

    def test_persistent_read_failure_is_bounded_and_next_cycle_can_recover(self):
        first, second, third = Mock(), Mock(), Mock()
        for api in (first, second):
            api.get_positions.side_effect = httpx.ConnectError('offline')
        factory = Mock(side_effect=[first, second, third])
        api = sdk_factory.ReconnectingAPI(factory, read_methods={'get_positions'})
        with self.assertRaises(httpx.ConnectError):
            api.get_positions()
        self.assertEqual(2, factory.call_count)
        api.get_positions()
        self.assertEqual(3, factory.call_count)

    def test_exchange_rejection_or_programming_error_is_not_transport_replayed(self):
        client = Mock()
        factory = Mock(return_value=client)
        api = sdk_factory.ReconnectingAPI(factory, read_methods={'get_positions'})
        client.get_positions.return_value = {'code':'50111', 'data':[]}
        self.assertEqual('50111', api.get_positions()['code'])
        client.get_positions.side_effect = ValueError('bad shape')
        with self.assertRaises(ValueError): api.get_positions()
        self.assertEqual(1, factory.call_count)


if __name__ == '__main__': unittest.main()
