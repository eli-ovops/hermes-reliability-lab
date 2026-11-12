from __future__ import annotations

from decimal import Decimal, InvalidOperation
import time


class GatewayError(RuntimeError):
    pass


class OKXGateway:
    """Small adapter over the installed python-okx SDK.

    Construction is dependency-injected so importing and testing this module never
    creates clients or opens a network connection.
    """

    def __init__(self, *, account_api, trade_api, market_api, public_api):
        self.account = account_api
        self.trade = trade_api
        self.market = market_api
        self.public = public_api

    def positions(self, symbol: str) -> list[dict]:
        data = _data(self.account.get_positions(instId=symbol), "positions")
        positions = []
        for item in data:
            try:
                if Decimal(str(item.get("pos") or "0")) != 0:
                    positions.append(item)
            except InvalidOperation as exc:
                raise GatewayError("持仓数量无效") from exc
        return positions

    def pending_orders(self, symbol: str) -> list[dict]:
        # Retry only this read. Never replay order placement/cancellation on
        # an ambiguous response, and never turn a failed query into an empty list.
        for attempt in range(3):
            try:
                return _data(
                    self.trade.get_order_list(instType="SWAP", instId=symbol),
                    "pending orders",
                )
            except Exception:
                if attempt == 2:
                    raise GatewayError("pending orders 查询连续3次失败") from None
                time.sleep(0.25 * (2 ** attempt))

    def available_usdt(self) -> Decimal:
        data = _data(self.account.get_account_balance(ccy="USDT"), "balance")
        for account in data:
            for detail in account.get("details") or []:
                if detail.get("ccy") == "USDT":
                    return Decimal(str(detail.get("availBal") or "0"))
        raise GatewayError("余额中缺少 USDT")

    def instrument(self, symbol: str) -> dict:
        data = _data(
            self.public.get_instruments(instType="SWAP", instId=symbol),
            "instrument",
        )
        matches = [item for item in data if item.get("instId") == symbol]
        if len(matches) != 1:
            raise GatewayError("无法唯一确定合约规格")
        return matches[0]

    def current_price(self, symbol: str) -> Decimal:
        data = _data(self.market.get_ticker(instId=symbol), "ticker")
        matches = [item for item in data if item.get("instId") == symbol]
        if len(matches) != 1:
            raise GatewayError("无法唯一确定当前价格")
        return Decimal(str(matches[0].get("last") or "0"))

    def ensure_leverage(self, symbol: str, direction: str, leverage: int) -> dict:
        return self.account.set_leverage(
            lever=str(leverage),
            mgnMode="isolated",
            instId=symbol,
            posSide=direction,
        )

    def cancel_order(self, symbol: str, ord_id: str) -> dict:
        return self.trade.cancel_order(instId=symbol, ordId=ord_id)

    def cancel_entry(self, symbol: str, ord_id: str) -> dict:
        return self.cancel_order(symbol, ord_id)

    def place_order(self, payload: dict) -> dict:
        return self.trade.place_order(**payload)

    def place_protection(self, payload: dict) -> dict:
        return self.trade.place_algo_order(**payload)

    def amend_protection(
        self, symbol: str, algo_id: str, new_size: str
    ) -> dict:
        return self.trade.amend_algo_order(
            instId=symbol,
            algoId=algo_id,
            cxlOnFail="true",
            newSz=new_size,
        )

    def order(
        self, symbol: str, *, ord_id: str | None = None, cl_ord_id: str | None = None
    ) -> dict:
        kwargs = {"instId": symbol}
        if ord_id:
            kwargs["ordId"] = ord_id
        else:
            kwargs["clOrdId"] = cl_ord_id or ""
        return self.trade.get_order(**kwargs)

    def pending_orders_response(self, symbol: str) -> dict:
        return self.trade.get_order_list(instType="SWAP", instId=symbol)

    def order_history_response(self, symbol: str) -> dict:
        return self.trade.get_orders_history(instType="SWAP", instId=symbol)

    def order_archive_response(self, symbol: str) -> dict:
        return self.trade.get_orders_history_archive(instType="SWAP", instId=symbol)

    def fills_response(self, symbol: str, *, ord_id: str | None = None) -> dict:
        return self.trade.get_fills(instType="SWAP", instId=symbol, ordId=ord_id or "")

    def positions_response(self, symbol: str) -> dict:
        return self.account.get_positions(instId=symbol)

    def algo_orders_response(self, symbol: str) -> dict:
        return self.trade.order_algos_list(
            ordType="oco", instType="SWAP", instId=symbol
        )

    def algo_history_response(self, symbol: str, *, algo_id: str) -> dict:
        return self.trade.order_algos_history(
            ordType="oco", algoId=algo_id, instId=symbol
        )


def _data(response: dict, operation: str) -> list:
    if not isinstance(response, dict) or response.get("code") != "0":
        raise GatewayError(f"{operation} 查询失败")
    data = response.get("data")
    if not isinstance(data, list):
        raise GatewayError(f"{operation} 返回结构无效")
    return data
