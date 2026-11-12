"""Read-only classification of orders and positions before a v2 cutover."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from src_v2.ledger import AttemptRecord, Ledger, LedgerConflict
from src_v2.parser import TradeSignal


class Ownership(str, Enum):
    V2_OWNED = "V2_OWNED"
    LEGACY_UNCLAIMED = "LEGACY_UNCLAIMED"
    FOREIGN = "FOREIGN"


class HandoffError(RuntimeError):
    pass


@dataclass(frozen=True)
class HandoffOrder:
    symbol: str
    order_id: str
    client_order_id: str
    ownership: Ownership
    state: str
    filled_size: str


@dataclass(frozen=True)
class HandoffReport:
    orders: tuple[HandoffOrder, ...]
    positions: tuple[dict, ...]

    @property
    def requires_user_decision(self) -> bool:
        return bool(
            self.positions
            or any(item.ownership is not Ownership.V2_OWNED for item in self.orders)
        )


def build_handoff_report(
    *, ledger: Ledger, gateway, symbols: tuple[str, ...]
) -> HandoffReport:
    """Read current exchange state and classify it without mutating either side."""
    orders: list[HandoffOrder] = []
    positions: list[dict] = []
    for symbol in dict.fromkeys(symbols):
        for order in gateway.pending_orders(symbol):
            client_order_id = str(order.get("clOrdId") or "")
            order_id = str(order.get("ordId") or "")
            attempt = (
                ledger.get_attempt_by_cl_ord_id(client_order_id)
                if client_order_id
                else None
            )
            if (
                attempt is not None
                and attempt.symbol == symbol
                and attempt.ord_id == order_id
            ):
                ownership = Ownership.V2_OWNED
            elif (
                order.get("tag") == "BANGZHU"
                or client_order_id.startswith("BZ")
            ):
                ownership = Ownership.LEGACY_UNCLAIMED
            else:
                ownership = Ownership.FOREIGN
            orders.append(
                HandoffOrder(
                    symbol=symbol,
                    order_id=order_id,
                    client_order_id=client_order_id,
                    ownership=ownership,
                    state=str(order.get("state") or ""),
                    filled_size=str(order.get("accFillSz") or "0"),
                )
            )
        positions.extend(dict(item) for item in gateway.positions(symbol))
    return HandoffReport(orders=tuple(orders), positions=tuple(positions))


def cancel_approved_legacy_orders(
    *,
    ledger: Ledger,
    gateway,
    symbols: tuple[str, ...],
    approved_order_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Cancel only exact, freshly-read, unfilled legacy orders approved by user."""
    if not approved_order_ids or len(set(approved_order_ids)) != len(
        approved_order_ids
    ):
        raise HandoffError("必须提供不重复的精确旧订单 ID")
    report = build_handoff_report(ledger=ledger, gateway=gateway, symbols=symbols)
    by_id = {item.order_id: item for item in report.orders}
    approved: list[HandoffOrder] = []
    for order_id in approved_order_ids:
        order = by_id.get(order_id)
        if order is None:
            raise HandoffError(f"批准的订单 {order_id} 已不在未成交列表")
        try:
            filled = Decimal(order.filled_size)
        except InvalidOperation as exc:
            raise HandoffError(f"订单 {order_id} 成交量无效") from exc
        if (
            order.ownership is not Ownership.LEGACY_UNCLAIMED
            or order.state != "live"
            or filled != 0
        ):
            raise HandoffError(f"订单 {order_id} 不是可安全清理的零成交旧单")
        approved.append(order)

    canceled: list[str] = []
    for order in approved:
        response = gateway.cancel_order(order.symbol, order.order_id)
        data = response.get("data") if isinstance(response, dict) else None
        if not (
            isinstance(response, dict)
            and response.get("code") == "0"
            and isinstance(data, list)
            and len(data) == 1
            and data[0].get("ordId") == order.order_id
            and data[0].get("sCode") in (None, "", "0")
        ):
            raise HandoffError(
                f"旧单撤销未确认；已完成 {','.join(canceled) or '0'}"
            )
        detail = gateway.order(
            order.symbol,
            ord_id=order.order_id,
            cl_ord_id=order.client_order_id,
        )
        detail_data = detail.get("data") if isinstance(detail, dict) else None
        item = detail_data[0] if isinstance(detail_data, list) and len(detail_data) == 1 else {}
        if not (
            isinstance(detail, dict)
            and detail.get("code") == "0"
            and item.get("instId") == order.symbol
            and item.get("ordId") == order.order_id
            and item.get("clOrdId") == order.client_order_id
            and item.get("state") in {"canceled", "mmp_canceled"}
            and str(item.get("accFillSz") or "0") in {"0", "0.0", ""}
        ):
            raise HandoffError(
                f"旧单撤销终态未知；已完成 {','.join(canceled) or '0'}"
            )
        canceled.append(order.order_id)
    return tuple(canceled)


def adopt_approved_legacy_order(
    *,
    ledger: Ledger,
    gateway,
    approved_order_id: str,
    strategy_id: str,
    source_chat_id: int,
    source_sender_id: int,
    source_message_id: int,
    raw_text: str,
    signal: TradeSignal,
    expected_size: str,
) -> AttemptRecord:
    """Adopt one exact protected, zero-fill legacy order without an OKX write."""
    if not approved_order_id.strip():
        raise HandoffError("必须提供精确旧订单 ID")
    try:
        size = Decimal(expected_size)
    except InvalidOperation as exc:
        raise HandoffError("批准的订单数量无效") from exc
    if size <= 0:
        raise HandoffError("批准的订单数量必须大于零")
    try:
        if gateway.positions(signal.symbol):
            raise HandoffError("存在持仓，禁止接管未成交旧单")
        pending = gateway.pending_orders(signal.symbol)
    except HandoffError:
        raise
    except Exception as exc:
        raise HandoffError("接管前账户状态查询失败") from exc
    matches = [item for item in pending if str(item.get("ordId") or "") == approved_order_id]
    if len(matches) != 1:
        raise HandoffError("批准的订单已不在唯一未成交列表")
    order = matches[0]
    _validate_adoptable_order(order, signal=signal, expected_size=size)
    try:
        detail = gateway.order(
            signal.symbol,
            ord_id=approved_order_id,
            cl_ord_id=str(order.get("clOrdId") or ""),
        )
    except Exception as exc:
        raise HandoffError("接管前订单详情查询失败") from exc
    detail_data = detail.get("data") if isinstance(detail, dict) else None
    if not (
        isinstance(detail, dict)
        and detail.get("code") == "0"
        and isinstance(detail_data, list)
        and len(detail_data) == 1
    ):
        raise HandoffError("接管前订单详情无法唯一确认")
    _validate_adoptable_order(detail_data[0], signal=signal, expected_size=size)
    if str(detail_data[0].get("ordId") or "") != approved_order_id:
        raise HandoffError("订单详情 ID 与批准 ID 不一致")
    try:
        return ledger.adopt_verified_live_order(
            strategy_id=strategy_id,
            source_chat_id=source_chat_id,
            source_sender_id=source_sender_id,
            source_message_id=source_message_id,
            raw_text=raw_text,
            signal=signal,
            cl_ord_id=str(order["clOrdId"]),
            ord_id=approved_order_id,
            requested_size=expected_size,
        )
    except (LedgerConflict, ValueError) as exc:
        raise HandoffError("接管订单写入账本失败") from exc


def _validate_adoptable_order(
    order: dict, *, signal: TradeSignal, expected_size: Decimal
) -> None:
    client_order_id = str(order.get("clOrdId") or "")
    expected_side = "buy" if signal.direction == "long" else "sell"
    try:
        price = Decimal(str(order.get("px") or ""))
        size = Decimal(str(order.get("sz") or ""))
        filled = Decimal(str(order.get("accFillSz") or "0"))
    except InvalidOperation as exc:
        raise HandoffError("旧订单价格、数量或成交量无效") from exc
    if size != expected_size:
        raise HandoffError("旧订单数量与批准数量不一致")
    if not (
        order.get("instId") == signal.symbol
        and order.get("tag") == "BANGZHU"
        and client_order_id.startswith("BZ")
        and order.get("side") == expected_side
        and order.get("posSide") == signal.direction
        and order.get("ordType") == "limit"
        and price == Decimal(str(signal.entry_price))
        and order.get("state") == "live"
        and filled == 0
    ):
        raise HandoffError("旧订单身份、方向、价格或状态不符合安全接管条件")
    attached = order.get("attachAlgoOrds")
    if not isinstance(attached, list) or len(attached) != 1:
        raise HandoffError("旧订单缺少唯一的止盈止损保护")
    protection = attached[0]
    try:
        take_profit = Decimal(str(protection.get("tpTriggerPx") or ""))
        stop_loss = Decimal(str(protection.get("slTriggerPx") or ""))
    except InvalidOperation as exc:
        raise HandoffError("旧订单止盈止损价格无效") from exc
    if not (
        protection.get("attachAlgoClOrdId") == client_order_id + "P"
        and take_profit == Decimal(str(signal.target))
        and stop_loss == Decimal(str(signal.stop))
        and str(protection.get("tpOrdPx") or "") == "-1"
        and str(protection.get("slOrdPx") or "") == "-1"
    ):
        raise HandoffError("旧订单止盈止损与批准策略不一致")
