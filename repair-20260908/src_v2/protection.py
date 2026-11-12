from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from src_v2.ledger import Ledger


class ProtectionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True)
class ProtectionResult:
    status: ProtectionStatus
    reason: str = ""


class ProtectionManager:
    def __init__(self, *, ledger: Ledger, gateway):
        self.ledger = ledger
        self.gateway = gateway

    def ensure_coverage(self, attempt_id: str) -> ProtectionResult:
        attempt = self.ledger.get_attempt(attempt_id)
        signal = self.ledger.get_attempt_signal(attempt_id)
        if attempt is None or signal is None:
            return ProtectionResult(ProtectionStatus.NEEDS_REVIEW, "缺少订单或策略记录")
        filled = Decimal(attempt.filled_size)
        if filled <= 0:
            return ProtectionResult(ProtectionStatus.NEEDS_REVIEW, "尚无已成交数量")
        existing = self.ledger.get_protection(attempt_id)
        if existing is not None:
            covered = Decimal(existing.covered_size)
            if existing.state != "ACTIVE":
                return ProtectionResult(
                    ProtectionStatus.NEEDS_REVIEW, "保护单账本状态不是 ACTIVE"
                )
            if covered == filled:
                return ProtectionResult(ProtectionStatus.ACTIVE)
            if covered > filled:
                return ProtectionResult(
                    ProtectionStatus.NEEDS_REVIEW, "保护数量大于已成交数量"
                )
            try:
                response = self.gateway.amend_protection(
                    attempt.symbol,
                    existing.algo_id,
                    _decimal_text(filled),
                )
            except Exception:
                recovered = self._recover_active_algo(
                    attempt, filled, expected_algo_id=existing.algo_id
                )
                if recovered:
                    return ProtectionResult(ProtectionStatus.ACTIVE)
                return self._cancel_unfilled_remainder(
                    attempt, "保护单扩量结果不确定"
                )
            if _successful_algo_id(response) != existing.algo_id:
                recovered = self._recover_active_algo(
                    attempt, filled, expected_algo_id=existing.algo_id
                )
                if recovered:
                    return ProtectionResult(ProtectionStatus.ACTIVE)
                return self._cancel_unfilled_remainder(
                    attempt, "保护单扩量未得到可验证确认"
                )
            self.ledger.record_protection(
                attempt_id,
                algo_id=existing.algo_id,
                covered_size=_decimal_text(filled),
                state="ACTIVE",
            )
            return ProtectionResult(ProtectionStatus.ACTIVE)
        payload = {
            "instId": attempt.symbol,
            "tdMode": "isolated",
            "side": "sell" if attempt.direction == "long" else "buy",
            "posSide": attempt.direction,
            "ordType": "oco",
            "sz": _decimal_text(filled),
            "reduceOnly": "true",
            "algoClOrdId": attempt.cl_ord_id + "P",
            "tpTriggerPx": _decimal_text(Decimal(str(signal.target))),
            "tpOrdPx": "-1",
            "tpTriggerPxType": "mark",
            "slTriggerPx": _decimal_text(Decimal(str(signal.stop))),
            "slOrdPx": "-1",
            "slTriggerPxType": "mark",
        }
        try:
            response = self.gateway.place_protection(payload)
        except Exception:
            if self._recover_active_algo(attempt, filled):
                return ProtectionResult(ProtectionStatus.ACTIVE)
            return self._cancel_unfilled_remainder(
                attempt, "保护单提交结果不确定"
            )
        algo_id = _successful_algo_id(response)
        if algo_id is None:
            if self._recover_active_algo(attempt, filled):
                return ProtectionResult(ProtectionStatus.ACTIVE)
            return self._cancel_unfilled_remainder(attempt, "保护单未返回 algoId")
        self.ledger.record_protection(
            attempt_id,
            algo_id=algo_id,
            covered_size=_decimal_text(filled),
            state="ACTIVE",
        )
        return ProtectionResult(ProtectionStatus.ACTIVE)

    def _recover_active_algo(
        self, attempt, filled: Decimal, *, expected_algo_id: str | None = None
    ) -> bool:
        try:
            response = self.gateway.algo_orders_response(attempt.symbol)
        except Exception:
            return False
        if not isinstance(response, dict) or response.get("code") != "0":
            return False
        data = response.get("data")
        if not isinstance(data, list):
            return False
        matches = []
        for item in data:
            try:
                size_matches = Decimal(str(item.get("sz"))) == filled
            except Exception:
                size_matches = False
            if (
                item.get("instId") == attempt.symbol
                and item.get("algoClOrdId") == attempt.cl_ord_id + "P"
                and item.get("ordType") == "oco"
                and item.get("posSide") == attempt.direction
                and item.get("algoId")
                and (expected_algo_id is None or item.get("algoId") == expected_algo_id)
                and size_matches
            ):
                matches.append(item)
        if len(matches) != 1:
            return False
        self.ledger.record_protection(
            attempt.attempt_id,
            algo_id=matches[0]["algoId"],
            covered_size=_decimal_text(filled),
            state="ACTIVE",
        )
        return True

    def _cancel_unfilled_remainder(self, attempt, reason: str) -> ProtectionResult:
        if not attempt.ord_id:
            return ProtectionResult(ProtectionStatus.NEEDS_REVIEW, reason)
        if attempt.state in {"FILLED", "CANCELED_PARTIAL"}:
            self.ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=(attempt.state,),
                new_state="PROTECTION_FAILED",
                error=reason,
            )
            return ProtectionResult(
                ProtectionStatus.NEEDS_REVIEW,
                f"{reason}；入场已无未成交余量，现有仓位仍需紧急处理",
            )
        try:
            self.gateway.cancel_entry(attempt.symbol, attempt.ord_id)
        except Exception:
            pass
        try:
            detail = self.gateway.order(
                attempt.symbol,
                ord_id=attempt.ord_id,
                cl_ord_id=attempt.cl_ord_id,
            )
        except Exception:
            detail = None
        cancel_confirmed = _confirmed_partial_cancel(detail, attempt)
        self.ledger.transition_attempt(
            attempt.attempt_id,
            expected_states=("PARTIALLY_FILLED", "FILLED", "CANCELED_PARTIAL"),
            new_state="PROTECTION_FAILED" if cancel_confirmed else "UNKNOWN",
            error=reason,
        )
        suffix = "已撤销未成交余量" if cancel_confirmed else "未能确认未成交余量已撤销"
        return ProtectionResult(ProtectionStatus.NEEDS_REVIEW, f"{reason}；{suffix}")


def _successful_algo_id(response: dict) -> str | None:
    if not isinstance(response, dict) or response.get("code") != "0":
        return None
    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1:
        return None
    item = data[0]
    if item.get("sCode") not in (None, "", "0"):
        return None
    return item.get("algoId") or None


def _confirmed_partial_cancel(response: dict | None, attempt) -> bool:
    if not isinstance(response, dict) or response.get("code") != "0":
        return False
    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1:
        return False
    item = data[0]
    try:
        fill_matches = Decimal(str(item.get("accFillSz") or "0")) == Decimal(
            attempt.filled_size
        )
    except Exception:
        fill_matches = False
    return (
        item.get("instId") == attempt.symbol
        and item.get("ordId") == attempt.ord_id
        and item.get("clOrdId") == attempt.cl_ord_id
        and item.get("state") in {"canceled", "mmp_canceled"}
        and fill_matches
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
