from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum

from src_v2.ledger import Ledger
from src_v2.protection import ProtectionManager, ProtectionStatus


class ReconcileStatus(str, Enum):
    LIVE = "LIVE"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    CLOSED = "CLOSED"
    MISSING_CONFIRMED = "MISSING_CONFIRMED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True)
class ReconcileResult:
    status: ReconcileStatus
    reason: str = ""


class Reconciler:
    def __init__(
        self,
        *,
        ledger: Ledger,
        gateway,
        now_provider=None,
        missing_grace: timedelta = timedelta(minutes=2),
    ):
        self.ledger = ledger
        self.gateway = gateway
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.missing_grace = missing_grace

    def reconcile(self, attempt_id: str) -> ReconcileResult:
        attempt = self.ledger.get_attempt(attempt_id)
        if attempt is None:
            return ReconcileResult(ReconcileStatus.NEEDS_REVIEW, "账本中不存在该订单尝试")
        if attempt.state == "MISSING_CONFIRMED":
            return ReconcileResult(
                ReconcileStatus.MISSING_CONFIRMED,
                "账本已完成全链缺失确认",
            )
        if attempt.state == "CANCELED":
            return ReconcileResult(ReconcileStatus.CANCELED, "账本已确认订单撤销")
        last_error: Exception | None = None
        for _ in range(2):
            try:
                response = self.gateway.order(
                    attempt.symbol,
                    ord_id=attempt.ord_id,
                    cl_ord_id=attempt.cl_ord_id,
                )
            except Exception as exc:
                last_error = exc
                continue
            break
        else:
            error_type = type(last_error).__name__ if last_error else "UnknownError"
            return ReconcileResult(
                ReconcileStatus.NEEDS_REVIEW,
                f"订单详情查询异常 ({error_type})",
            )
        if _response_ok(response):
            order = _find_order(response["data"], attempt)
            if order is not None:
                return self._apply_order(attempt, order)
            if response["data"]:
                return ReconcileResult(
                    ReconcileStatus.NEEDS_REVIEW,
                    "订单详情返回了不匹配的订单",
                )
        elif not _is_not_found(response):
            return ReconcileResult(ReconcileStatus.NEEDS_REVIEW, "订单详情查询失败")

        for method_name in (
            "pending_orders_response",
            "order_history_response",
            "order_archive_response",
        ):
            try:
                candidate_response = getattr(self.gateway, method_name)(attempt.symbol)
            except Exception:
                return ReconcileResult(
                    ReconcileStatus.NEEDS_REVIEW,
                    f"{method_name} 查询异常",
                )
            if not _response_ok(candidate_response):
                return ReconcileResult(
                    ReconcileStatus.NEEDS_REVIEW,
                    f"{method_name} 查询失败",
                )
            order = _find_order(candidate_response["data"], attempt)
            if order is not None:
                return self._apply_order(attempt, order)

        read_methods = (
            ("fills_response", {"ord_id": attempt.ord_id}),
            ("positions_response", {}),
            ("algo_orders_response", {}),
        )
        residual = []
        for method_name, keywords in read_methods:
            try:
                candidate_response = getattr(self.gateway, method_name)(
                    attempt.symbol, **keywords
                )
            except Exception:
                return ReconcileResult(
                    ReconcileStatus.NEEDS_REVIEW, f"{method_name} 查询异常"
                )
            if not _response_ok(candidate_response):
                return ReconcileResult(
                    ReconcileStatus.NEEDS_REVIEW, f"{method_name} 查询失败"
                )
            residual.extend(candidate_response["data"])
        if _has_related_residual(residual, attempt):
            return ReconcileResult(
                ReconcileStatus.NEEDS_REVIEW,
                "订单列表缺失，但成交、持仓或保护单仍有相关记录",
            )
        try:
            last_transition = datetime.fromisoformat(attempt.updated_at)
            age = self.now_provider() - last_transition
        except (TypeError, ValueError):
            return ReconcileResult(
                ReconcileStatus.NEEDS_REVIEW,
                "账本时间无效，禁止确认订单缺失",
            )
        if age < self.missing_grace:
            return ReconcileResult(
                ReconcileStatus.NEEDS_REVIEW,
                "订单仍在 2 分钟传播确认窗口内，禁止重试",
            )
        cancellation_recovery = attempt.state in {"CANCELING", "CANCEL_UNKNOWN"}
        self.ledger.transition_attempt(
            attempt.attempt_id,
            expected_states=(
                "SUBMITTING",
                "SUBMITTED",
                "UNKNOWN",
                "CANCELING",
                "CANCEL_UNKNOWN",
            ),
            new_state="CANCELED" if cancellation_recovery else "MISSING_CONFIRMED",
        )
        return ReconcileResult(
            ReconcileStatus.CANCELED if cancellation_recovery else ReconcileStatus.MISSING_CONFIRMED,
            (
                "撤单恢复链确认订单已不存在"
                if cancellation_recovery
                else "六条读取路径均确认订单不存在"
            ),
        )

    def _apply_order(self, attempt, order: dict) -> ReconcileResult:
        state = order.get("state")
        filled_size = order.get("accFillSz") or order.get("fillSz") or "0"
        state_map = {
            "live": ("LIVE", ReconcileStatus.LIVE),
            "partially_filled": ("PARTIALLY_FILLED", ReconcileStatus.PARTIALLY_FILLED),
            "filled": ("FILLED", ReconcileStatus.FILLED),
            "canceled": ("CANCELED", ReconcileStatus.CANCELED),
            "mmp_canceled": ("CANCELED", ReconcileStatus.CANCELED),
        }
        if state not in state_map:
            return ReconcileResult(
                ReconcileStatus.NEEDS_REVIEW,
                f"订单状态 {state} 需要额外保护单核验",
            )
        ledger_state, result_state = state_map[state]
        if ledger_state == "FILLED" and self._old_fill_is_confirmed_flat(
            attempt, order
        ):
            self.ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=(
                    "SUBMITTING",
                    "SUBMITTED",
                    "UNKNOWN",
                    "LIVE",
                    "PARTIALLY_FILLED",
                    "FILLED",
                    "PROTECTION_FAILED",
                ),
                new_state="CLOSED",
                ord_id=order["ordId"],
                filled_size=filled_size,
            )
            return ReconcileResult(
                ReconcileStatus.CLOSED,
                "历史成交已超过传播窗口且对应方向仓位为 0",
            )
        if ledger_state == "CANCELED" and filled_size not in {"", "0", "0.0"}:
            self.ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=(
                    "SUBMITTING",
                    "SUBMITTED",
                    "UNKNOWN",
                    "LIVE",
                    "PARTIALLY_FILLED",
                    "CANCELED_PARTIAL",
                    "PROTECTION_FAILED",
                ),
                new_state="CANCELED_PARTIAL",
                ord_id=order["ordId"],
                filled_size=filled_size,
            )
            protection = ProtectionManager(
                ledger=self.ledger, gateway=self.gateway
            ).ensure_coverage(attempt.attempt_id)
            if protection.status != ProtectionStatus.ACTIVE:
                return ReconcileResult(
                    ReconcileStatus.NEEDS_REVIEW,
                    f"订单撤销前已有成交；{protection.reason}",
                )
            verified = self._verify_protection(attempt.attempt_id)
            if verified is not None:
                return verified
            return ReconcileResult(
                ReconcileStatus.NEEDS_REVIEW,
                "订单撤销前已有成交；仓位已保护，需询问用户",
            )
        self.ledger.transition_attempt(
            attempt.attempt_id,
            expected_states=(
                "SUBMITTING",
                "SUBMITTED",
                "UNKNOWN",
                "LIVE",
                "PARTIALLY_FILLED",
                "FILLED",
                "CANCELING",
                "CANCEL_UNKNOWN",
                "PROTECTION_FAILED",
            ),
            new_state=ledger_state,
            ord_id=order["ordId"],
            filled_size=filled_size,
        )
        if ledger_state in {"PARTIALLY_FILLED", "FILLED"}:
            protection = ProtectionManager(
                ledger=self.ledger, gateway=self.gateway
            ).ensure_coverage(attempt.attempt_id)
            if protection.status != ProtectionStatus.ACTIVE:
                return ReconcileResult(
                    ReconcileStatus.NEEDS_REVIEW,
                    protection.reason,
                )
            verified = self._verify_protection(attempt.attempt_id)
            if verified is not None:
                return verified
        return ReconcileResult(result_state)

    def _old_fill_is_confirmed_flat(self, attempt, order: dict) -> bool:
        if self.ledger.get_protection(attempt.attempt_id) is not None:
            return False
        timestamp = order.get("fillTime") or order.get("uTime")
        try:
            filled_at = datetime.fromtimestamp(
                int(str(timestamp)) / 1000, tz=timezone.utc
            )
            now = self.now_provider()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, OSError):
            return False
        if now - filled_at < self.missing_grace:
            return False
        try:
            positions = self.gateway.positions_response(attempt.symbol)
            algos = self.gateway.algo_orders_response(attempt.symbol)
        except Exception:
            return False
        if not _response_ok(positions) or not _response_ok(algos):
            return False
        for position in positions["data"]:
            if (
                position.get("instId") == attempt.symbol
                and position.get("posSide") == attempt.direction
            ):
                try:
                    if Decimal(str(position.get("pos") or "0")) != 0:
                        return False
                except InvalidOperation:
                    return False
        expected_algo_cl_ord_id = attempt.cl_ord_id + "P"
        if any(
            item.get("instId") == attempt.symbol
            and item.get("posSide") == attempt.direction
            and item.get("algoClOrdId") == expected_algo_cl_ord_id
            for item in algos["data"]
        ):
            return False
        return True

    def _verify_protection(self, attempt_id: str) -> ReconcileResult | None:
        attempt = self.ledger.get_attempt(attempt_id)
        protection = self.ledger.get_protection(attempt_id)
        if attempt is None or protection is None:
            return ReconcileResult(
                ReconcileStatus.NEEDS_REVIEW, "缺少保护单账本记录"
            )
        try:
            response = self.gateway.algo_orders_response(attempt.symbol)
        except Exception:
            return ReconcileResult(ReconcileStatus.NEEDS_REVIEW, "保护单查询异常")
        if not _response_ok(response):
            return ReconcileResult(ReconcileStatus.NEEDS_REVIEW, "保护单查询失败")
        matches = [
            item
            for item in response["data"]
            if item.get("algoId") == protection.algo_id
        ]
        if len(matches) != 1:
            terminal = self._close_from_algo_history(attempt, protection)
            if terminal is not None:
                return terminal
            return ReconcileResult(
                ReconcileStatus.NEEDS_REVIEW,
                "账本保护单未在 OKX 活跃或历史 Algo 订单中得到唯一确认",
            )
        item = matches[0]
        try:
            size_matches = (
                Decimal(str(item.get("sz"))) == Decimal(attempt.filled_size)
                == Decimal(protection.covered_size)
            )
        except (InvalidOperation, TypeError):
            size_matches = False
        identity_matches = (
            item.get("instId") == attempt.symbol
            and item.get("posSide") == attempt.direction
            and item.get("ordType") == "oco"
            and item.get("algoClOrdId") == attempt.cl_ord_id + "P"
        )
        if not identity_matches or not size_matches:
            return ReconcileResult(
                ReconcileStatus.NEEDS_REVIEW,
                "OKX 活跃保护单的归属或覆盖数量不匹配",
            )
        return None

    def _close_from_algo_history(self, attempt, protection) -> ReconcileResult | None:
        try:
            response = self.gateway.algo_history_response(
                attempt.symbol, algo_id=protection.algo_id
            )
        except Exception:
            return None
        if not _response_ok(response):
            return None
        matches = []
        for item in response["data"]:
            try:
                size_matches = Decimal(str(item.get("sz"))) == Decimal(
                    protection.covered_size
                )
            except (InvalidOperation, TypeError):
                size_matches = False
            if (
                item.get("algoId") == protection.algo_id
                and item.get("algoClOrdId") == attempt.cl_ord_id + "P"
                and item.get("instId") == attempt.symbol
                and item.get("posSide") == attempt.direction
                and item.get("ordType") == "oco"
                and item.get("state") in {
                    "effective",
                    "canceled",
                    "order_failed",
                }
                and size_matches
            ):
                matches.append(item)
        if len(matches) != 1:
            return None
        try:
            positions = self.gateway.positions_response(attempt.symbol)
        except Exception:
            return None
        if not _response_ok(positions):
            return None
        for item in positions["data"]:
            if item.get("instId") != attempt.symbol:
                continue
            if item.get("posSide") != attempt.direction:
                continue
            try:
                if Decimal(str(item.get("pos") or "0")) != 0:
                    return None
            except InvalidOperation:
                return None
        history_state = matches[0]["state"]
        self.ledger.record_protection(
            attempt.attempt_id,
            algo_id=protection.algo_id,
            covered_size=protection.covered_size,
            state="TRIGGERED" if history_state == "effective" else "CANCELED",
        )
        self.ledger.transition_attempt(
            attempt.attempt_id,
            expected_states=("FILLED", "CANCELED_PARTIAL"),
            new_state="CLOSED",
        )
        return ReconcileResult(
            ReconcileStatus.CLOSED,
            "保护单已进入历史终态且对应方向仓位为 0",
        )


def _response_ok(response: dict) -> bool:
    return (
        isinstance(response, dict)
        and response.get("code") == "0"
        and isinstance(response.get("data"), list)
    )


def _is_not_found(response: dict) -> bool:
    if not isinstance(response, dict):
        return False
    messages = [str(response.get("msg") or "")]
    for item in response.get("data") or []:
        messages.append(str(item.get("sMsg") or ""))
    text = " ".join(messages).lower()
    return response.get("code") == "51603" or "not exist" in text


def _find_order(items: list, attempt) -> dict | None:
    matches = [
        order
        for order in items
        if order.get("instId") == attempt.symbol
        and order.get("clOrdId") == attempt.cl_ord_id
        and order.get("ordId")
        and (not attempt.ord_id or order.get("ordId") == attempt.ord_id)
    ]
    return matches[0] if len(matches) == 1 else None


def _has_related_residual(items: list, attempt) -> bool:
    for item in items:
        if item.get("instId") != attempt.symbol:
            continue
        if item.get("clOrdId") == attempt.cl_ord_id:
            return True
        if attempt.ord_id and item.get("ordId") == attempt.ord_id:
            return True
        if item.get("algoClOrdId") in {
            attempt.cl_ord_id + "P",
            attempt.cl_ord_id + "TP",
            attempt.cl_ord_id + "SL",
        }:
            return True
        if item.get("posSide") == attempt.direction:
            try:
                if Decimal(str(item.get("pos") or "0")) != 0:
                    return True
            except InvalidOperation:
                return True
    return False
