from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from src_v2.ledger import Ledger
from src_v2.parser import TradeSignal
from src_v2.reconciler import ReconcileStatus, Reconciler
from src_v2.risk import RiskPolicy


class ExecutionStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ExecutionOutcome:
    status: ExecutionStatus
    placed: int = 0
    reason: str = ""


class Executor:
    def __init__(self, *, ledger: Ledger, gateway, risk_policy: RiskPolicy | None = None):
        self.ledger = ledger
        self.gateway = gateway
        self.risk_policy = risk_policy or RiskPolicy()

    def execute_strategy(
        self, strategy_id: str, signals: tuple[TradeSignal, ...]
    ) -> ExecutionOutcome:
        strategy = self.ledger.get_strategy(strategy_id)
        if strategy is None or strategy.state != "ACTIVE":
            return ExecutionOutcome(
                ExecutionStatus.NEEDS_REVIEW, reason="策略不存在或不是当前版本"
            )
        if not signals or {item.symbol for item in signals} != {strategy.symbol}:
            return ExecutionOutcome(
                ExecutionStatus.NEEDS_REVIEW, reason="策略币种与账本不一致"
            )
        if len({item.direction for item in signals}) != len(signals):
            return ExecutionOutcome(
                ExecutionStatus.NEEDS_REVIEW, reason="同一方向出现重复信号"
            )
        attempts = {item.direction: item for item in self.ledger.list_attempts(strategy_id)}
        if set(attempts) != {item.direction for item in signals}:
            return ExecutionOutcome(
                ExecutionStatus.NEEDS_REVIEW, reason="信号方向与账本不一致"
            )
        unsafe_states = {
            item.state
            for item in attempts.values()
            if item.state not in {"READY", "SUBMITTED", "LIVE", "PARTIALLY_FILLED", "FILLED"}
        }
        if unsafe_states:
            return ExecutionOutcome(
                ExecutionStatus.NEEDS_REVIEW,
                reason=f"存在不可直接执行的订单状态: {','.join(sorted(unsafe_states))}",
            )
        actionable_signals = tuple(
            signal for signal in signals if attempts[signal.direction].state == "READY"
        )
        if not actionable_signals:
            return ExecutionOutcome(ExecutionStatus.SUBMITTED, placed=0)
        try:
            positions = self.gateway.positions(strategy.symbol)
        except Exception:
            return ExecutionOutcome(
                ExecutionStatus.NEEDS_REVIEW,
                reason="持仓查询失败，保留全部订单且不下新单",
            )
        if positions and not _positions_belong_to_authorized_dual_strategy(
            self.ledger,
            strategy.symbol,
            attempts,
            positions,
        ):
            return ExecutionOutcome(
                ExecutionStatus.NEEDS_REVIEW, reason="已有持仓，保留仓位和挂单并询问用户"
            )
        try:
            instrument = self.gateway.instrument(strategy.symbol)
            current_price = self.gateway.current_price(strategy.symbol)
            plans = []
            for signal in actionable_signals:
                plans.append(
                    self.risk_policy.plan(
                        signal,
                        available_usdt=self.gateway.available_usdt(),
                        current_price=current_price,
                        instrument=instrument,
                    )
                )
        except Exception as exc:
            return ExecutionOutcome(ExecutionStatus.NEEDS_REVIEW, reason=str(exc))

        for signal, plan in zip(actionable_signals, plans, strict=True):
            try:
                leverage_response = self.gateway.ensure_leverage(
                    strategy.symbol, signal.direction, plan.leverage
                )
            except Exception:
                return ExecutionOutcome(
                    ExecutionStatus.NEEDS_REVIEW,
                    reason="设置杠杆时接口异常，旧单保持不动",
                )
            if not _response_succeeded(leverage_response):
                return ExecutionOutcome(
                    ExecutionStatus.NEEDS_REVIEW,
                    reason="无法确认杠杆设置，旧单保持不动",
                )

        try:
            pending_orders = self.gateway.pending_orders(strategy.symbol)
        except Exception:
            return ExecutionOutcome(
                ExecutionStatus.NEEDS_REVIEW,
                reason="未成交订单查询失败，旧单保持不动",
            )
        if any(
            order.get("instId") == strategy.symbol
            and order.get("state") == "partially_filled"
            for order in pending_orders
        ):
            return ExecutionOutcome(
                ExecutionStatus.NEEDS_REVIEW,
                reason="存在部分成交订单，保留仓位和全部挂单并询问用户",
            )
        cancellable = [
            order
            for order in pending_orders
            if self.ledger.is_cancellable_old_entry(
                order, replacing_strategy_id=strategy_id
            )
        ]
        known_client_ids = {
            attempt.cl_ord_id
            for attempt in attempts.values()
            if attempt.state in {"SUBMITTED", "LIVE", "PARTIALLY_FILLED", "FILLED"}
        } | {order["clOrdId"] for order in cancellable}
        orphaned_system_orders = [
            order
            for order in pending_orders
            if (
                order.get("tag") == "BANGZHU"
                or str(order.get("clOrdId") or "").startswith("BZ")
            )
            and order.get("clOrdId") not in known_client_ids
        ]
        if orphaned_system_orders:
            return ExecutionOutcome(
                ExecutionStatus.NEEDS_REVIEW,
                reason="发现无法由账本证明归属的 BANGZHU 挂单，禁止叠加新单",
            )
        for order in cancellable:
            old_attempt = self.ledger.get_attempt_by_cl_ord_id(order["clOrdId"])
            if old_attempt is None:
                return ExecutionOutcome(
                    ExecutionStatus.NEEDS_REVIEW,
                    reason="旧订单通过筛选后却无法在账本中读回",
                )
            self.ledger.transition_attempt(
                old_attempt.attempt_id,
                expected_states=("LIVE",),
                new_state="CANCELING",
            )
            try:
                response = self.gateway.cancel_order(strategy.symbol, order["ordId"])
            except Exception:
                self.ledger.transition_attempt(
                    old_attempt.attempt_id,
                    expected_states=("CANCELING",),
                    new_state="CANCEL_UNKNOWN",
                    error="撤单响应丢失",
                )
                return ExecutionOutcome(
                    ExecutionStatus.UNKNOWN,
                    reason=f"旧订单 {order['ordId']} 撤销响应丢失",
                )
            if not _cancel_acknowledged(response, order["ordId"]):
                self.ledger.transition_attempt(
                    old_attempt.attempt_id,
                    expected_states=("CANCELING",),
                    new_state="CANCEL_UNKNOWN",
                    error="撤单响应未得到确认",
                )
                return ExecutionOutcome(
                    ExecutionStatus.UNKNOWN,
                    reason=f"无法确认旧订单 {order['ordId']} 已撤销",
                )
            try:
                detail = self.gateway.order(
                    strategy.symbol,
                    ord_id=order["ordId"],
                    cl_ord_id=order["clOrdId"],
                )
            except Exception:
                detail = None
            if not _is_confirmed_unfilled_cancel(detail, order):
                self.ledger.transition_attempt(
                    old_attempt.attempt_id,
                    expected_states=("CANCELING",),
                    new_state="CANCEL_UNKNOWN",
                    error="撤单回执后未确认终态",
                )
                return ExecutionOutcome(
                    ExecutionStatus.UNKNOWN,
                    reason=f"旧订单 {order['ordId']} 尚未确认进入已撤销终态",
                )
            self.ledger.transition_attempt(
                old_attempt.attempt_id,
                expected_states=("CANCELING",),
                new_state="CANCELED",
            )

        placed = 0
        for signal in actionable_signals:
            attempt = attempts[signal.direction]
            try:
                available_usdt = self.gateway.available_usdt()
                plan = self.risk_policy.plan(
                    signal,
                    available_usdt=available_usdt,
                    current_price=self.gateway.current_price(strategy.symbol),
                    instrument=instrument,
                )
            except Exception as exc:
                return ExecutionOutcome(ExecutionStatus.NEEDS_REVIEW, placed, str(exc))
            self.ledger.record_order_plan_snapshot(
                attempt.attempt_id,
                available_usdt=_decimal_text(available_usdt),
                leverage=plan.leverage,
                price=_decimal_text(plan.price),
                size=_decimal_text(plan.size),
                margin_fraction=_decimal_text(plan.margin_fraction),
            )
            self.ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="SUBMITTING",
                requested_size=_decimal_text(plan.size),
            )
            order = _order_payload(attempt.cl_ord_id, plan)
            try:
                response = self.gateway.place_order(order)
            except Exception as exc:
                self.ledger.transition_attempt(
                    attempt.attempt_id,
                    expected_states=("SUBMITTING",),
                    new_state="UNKNOWN",
                    error=f"{type(exc).__name__}: 下单响应丢失",
                )
                return ExecutionOutcome(
                    ExecutionStatus.UNKNOWN,
                    placed,
                    "下单响应丢失，禁止自动重试",
                )
            ord_id = _placed_order_id(response, attempt.cl_ord_id)
            if ord_id is None:
                self.ledger.transition_attempt(
                    attempt.attempt_id,
                    expected_states=("SUBMITTING",),
                    new_state="UNKNOWN",
                    error="下单响应缺少可验证的 ordId",
                )
                return ExecutionOutcome(
                    ExecutionStatus.UNKNOWN,
                    placed,
                    "下单结果不确定，禁止自动重试",
                )
            self.ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("SUBMITTING",),
                new_state="SUBMITTED",
                ord_id=ord_id,
            )
            placed += 1
            reconciliation = Reconciler(
                ledger=self.ledger, gateway=self.gateway
            ).reconcile(attempt.attempt_id)
            if reconciliation.status not in {
                ReconcileStatus.LIVE,
                ReconcileStatus.PARTIALLY_FILLED,
                ReconcileStatus.FILLED,
            }:
                return ExecutionOutcome(
                    ExecutionStatus.UNKNOWN,
                    placed,
                    "订单已提交，但即时对账或仓位保护未闭环；禁止继续下单",
                )
        return ExecutionOutcome(ExecutionStatus.SUBMITTED, placed=placed)


def _response_succeeded(response: dict) -> bool:
    if not isinstance(response, dict) or response.get("code") != "0":
        return False
    data = response.get("data")
    if not isinstance(data, list) or not data:
        return False
    for item in data:
        if item.get("sCode") not in (None, "", "0"):
            return False
    return True


def _cancel_acknowledged(response: dict, expected_ord_id: str) -> bool:
    if not isinstance(response, dict) or response.get("code") != "0":
        return False
    data = response.get("data")
    return bool(
        isinstance(data, list)
        and len(data) == 1
        and data[0].get("ordId") == expected_ord_id
        and data[0].get("sCode") in (None, "", "0")
    )


def _placed_order_id(response: dict, expected_cl_ord_id: str) -> str | None:
    if not isinstance(response, dict) or response.get("code") != "0":
        return None
    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1:
        return None
    item = data[0]
    if (
        item.get("sCode") not in (None, "", "0")
        or item.get("clOrdId") != expected_cl_ord_id
        or not item.get("ordId")
    ):
        return None
    return str(item["ordId"])


def _is_confirmed_unfilled_cancel(response: dict | None, order: dict) -> bool:
    if not isinstance(response, dict) or response.get("code") != "0":
        return False
    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1:
        return False
    item = data[0]
    return (
        item.get("instId") == order.get("instId")
        and item.get("ordId") == order.get("ordId")
        and item.get("clOrdId") == order.get("clOrdId")
        and item.get("state") in {"canceled", "mmp_canceled"}
        and str(item.get("accFillSz") or "0") in {"0", "0.0", ""}
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _order_payload(cl_ord_id: str, plan) -> dict:
    payload = {
        "instId": plan.symbol,
        "tdMode": "isolated",
        "side": "buy" if plan.direction == "long" else "sell",
        "ordType": plan.order_type,
        "sz": _decimal_text(plan.size),
        "posSide": plan.direction,
        "clOrdId": cl_ord_id,
        "tag": "BANGZHU",
    }
    if plan.order_type == "limit":
        payload["px"] = _decimal_text(plan.price)
    return payload


def _positions_belong_to_authorized_dual_strategy(
    ledger: Ledger,
    symbol: str,
    attempts: dict,
    positions: list[dict],
) -> bool:
    if set(attempts) != {"long", "short"}:
        return False
    for position in positions:
        direction = position.get("posSide")
        if position.get("instId") != symbol or direction not in attempts:
            return False
        attempt = attempts[direction]
        if attempt.state not in {"PARTIALLY_FILLED", "FILLED"}:
            return False
        protection = ledger.get_protection(attempt.attempt_id)
        if protection is None or protection.state != "ACTIVE":
            return False
        try:
            position_size = abs(Decimal(str(position.get("pos") or "0")))
            filled_size = Decimal(attempt.filled_size)
            covered_size = Decimal(protection.covered_size)
        except InvalidOperation:
            return False
        if position_size <= 0 or filled_size < position_size or covered_size < position_size:
            return False
    return True
