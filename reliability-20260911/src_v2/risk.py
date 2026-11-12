from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from src_v2.parser import TradeSignal


class RiskReview(RuntimeError):
    """The plan cannot be executed without a user decision."""


@dataclass(frozen=True)
class OrderPlan:
    symbol: str
    direction: str
    leverage: int
    order_type: str
    price: Decimal
    size: Decimal
    stop: Decimal
    target: Decimal
    margin_fraction: Decimal


def _round_step(value: Decimal, step: Decimal, rounding) -> Decimal:
    if step <= 0:
        raise RiskReview("交易规格步长无效")
    return (value / step).to_integral_value(rounding=rounding) * step


class RiskPolicy:
    margin_fraction = Decimal("0.01")
    maximum_chase_deviation = Decimal("0.02")
    major_leverage = 50
    alt_leverage = 10
    major_symbols = frozenset({"BTC-USDT-SWAP", "ETH-USDT-SWAP"})

    def leverage_for(self, symbol: str) -> int:
        return self.major_leverage if symbol in self.major_symbols else self.alt_leverage

    def plan(
        self,
        signal: TradeSignal,
        *,
        available_usdt: Decimal,
        current_price: Decimal,
        instrument: dict,
    ) -> OrderPlan:
        if available_usdt <= 0 or current_price <= 0:
            raise RiskReview("可用 USDT 或当前价格无效")
        entry = Decimal(str(signal.entry_price))
        stop = Decimal(str(signal.stop))
        target = Decimal(str(signal.target))
        tick = Decimal(str(instrument["tickSz"]))
        lot = Decimal(str(instrument["lotSz"]))
        minimum = Decimal(str(instrument["minSz"]))
        contract_value = Decimal(str(instrument["ctVal"]))
        multiplier = Decimal(str(instrument.get("ctMult") or "1"))
        if signal.direction == "long":
            valid_prices = stop < entry < target
            crossed = current_price <= entry
        elif signal.direction == "short":
            valid_prices = target < entry < stop
            crossed = current_price >= entry
        else:
            raise RiskReview("交易方向无效")
        if not valid_prices:
            raise RiskReview("入场、止损和止盈方向矛盾")

        order_type = "limit"
        execution_price = entry
        if crossed:
            deviation = abs(current_price - entry) / entry
            if deviation > self.maximum_chase_deviation:
                raise RiskReview("当前价格与原入场价相差超过 2%")
            executable_now = (
                stop < current_price < target
                if signal.direction == "long"
                else target < current_price < stop
            )
            if not executable_now:
                raise RiskReview("当前价格已越过止损或止盈，禁止追单")
            order_type = "market"
            execution_price = current_price

        rounded_price = _round_step(execution_price, tick, ROUND_HALF_UP)
        rounded_stop = _round_step(stop, tick, ROUND_HALF_UP)
        rounded_target = _round_step(target, tick, ROUND_HALF_UP)
        if not (
            rounded_stop < rounded_price < rounded_target if signal.direction == "long"
            else rounded_target < rounded_price < rounded_stop
        ):
            raise RiskReview("价格按最小价位取整后止损/止盈方向无效")
        leverage = self.leverage_for(signal.symbol)
        contract_notional = rounded_price * contract_value * multiplier
        if contract_notional <= 0:
            raise RiskReview("合约面值无效")
        target_notional = available_usdt * self.margin_fraction * Decimal(leverage)
        size = _round_step(target_notional / contract_notional, lot, ROUND_DOWN)
        if size < minimum:
            minimum_margin = minimum * contract_notional / Decimal(leverage)
            minimum_fraction = minimum_margin / available_usdt
            if minimum_fraction > self.margin_fraction:
                raise RiskReview("交易所最小下单量会使保证金超过1%，需用户确认")
            size = minimum
        actual_margin = size * contract_notional / Decimal(leverage)
        actual_fraction = actual_margin / available_usdt
        if actual_fraction > self.margin_fraction:
            raise RiskReview("订单规格取整后保证金超过1%，需用户确认")
        return OrderPlan(
            symbol=signal.symbol,
            direction=signal.direction,
            leverage=leverage,
            order_type=order_type,
            price=rounded_price,
            size=size,
            stop=rounded_stop,
            target=rounded_target,
            margin_fraction=actual_fraction,
        )
