from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import time

from src_v2.attached_protection import expected_protection, algo_matches
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
        if self.ledger.get_attachment_intent(attempt_id):
            return self._ensure_native(attempt)
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
        try:
            expected = expected_protection(self.ledger, self.gateway, attempt)
            response = self.gateway.algo_orders_response(attempt.symbol)
            if not isinstance(response, dict) or response.get("code") != "0" or not isinstance(response.get("data"), list):
                raise ValueError("Invalid algo read")
        except Exception:
            return ProtectionResult(ProtectionStatus.NEEDS_REVIEW, "保护参数或活跃保护单查询失败，禁止重复提交")
        related = [d for d in response["data"] if d.get("algoClOrdId") == attempt.cl_ord_id + "P"]
        if related:
            if len(related) == 1 and algo_matches(related[0], attempt, expected, filled):
                self.ledger.record_protection(attempt_id, algo_id=related[0]["algoId"], covered_size=_decimal_text(filled), state="ACTIVE")
                return ProtectionResult(ProtectionStatus.ACTIVE)
            return ProtectionResult(ProtectionStatus.NEEDS_REVIEW, "已有保护单参数冲突，禁止重复提交")
        payload = {
            "instId": attempt.symbol,
            "tdMode": "isolated",
            "side": "sell" if attempt.direction == "long" else "buy",
            "posSide": attempt.direction,
            "ordType": "oco",
            "sz": _decimal_text(filled),
            "reduceOnly": "true",
            "algoClOrdId": attempt.cl_ord_id + "P",
            "tpTriggerPx": expected["tpTriggerPx"],
            "tpOrdPx": "-1",
            "tpTriggerPxType": "mark",
            "slTriggerPx": expected["slTriggerPx"],
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

    def _ensure_native(self, attempt) -> ProtectionResult:
        # OKX activates an attached OCO after full fill or partial fill + remainder cancel.
        # Never place a standalone duplicate while native activation may be in flight.
        if attempt.state == "PARTIALLY_FILLED":
            try:
                self.gateway.cancel_entry(attempt.symbol, attempt.ord_id)
            except Exception:
                pass
            try:
                response = self.gateway.order(attempt.symbol, ord_id=attempt.ord_id, cl_ord_id=attempt.cl_ord_id)
                rows = response.get("data") if response.get("code") == "0" else []
                if not isinstance(rows, list) or len(rows) != 1:
                    raise ValueError("Invalid order detail")
                item = rows[0]
                if (item.get("ordId") != attempt.ord_id or item.get("clOrdId") != attempt.cl_ord_id
                    or item.get("instId") != attempt.symbol or item.get("state") not in {"filled","canceled","mmp_canceled"}):
                    raise ValueError("Unconfirmed remainder cancellation")
                filled = Decimal(str(item["accFillSz"]))
                if not filled.is_finite() or filled < Decimal(attempt.filled_size) or filled > Decimal(attempt.requested_size):
                    raise ValueError("Invalid accumulated fill")
                self.ledger.transition_attempt(attempt.attempt_id, expected_states=("PARTIALLY_FILLED",),
                    new_state="FILLED" if item["state"] == "filled" else "CANCELED_PARTIAL", filled_size=_decimal_text(filled))
                attempt = self.ledger.get_attempt(attempt.attempt_id)
            except Exception:
                return ProtectionResult(ProtectionStatus.NEEDS_REVIEW, "原生保护等待部分成交余单撤销确认，禁止重复保护")
        existing = self.ledger.get_protection(attempt.attempt_id)
        if existing is not None and existing.state == "ACTIVE" and Decimal(existing.covered_size) == Decimal(attempt.filled_size):
            # Reconciler must verify active terms or a terminal algo + flat position.
            return ProtectionResult(ProtectionStatus.ACTIVE)
        for read_attempt in range(3):
            if read_attempt:
                time.sleep(0.25 * read_attempt)
            try:
                if self._recover_active_algo(attempt, Decimal(attempt.filled_size)):
                    return ProtectionResult(ProtectionStatus.ACTIVE)
            except Exception:
                continue
        return ProtectionResult(ProtectionStatus.NEEDS_REVIEW, "原生止盈止损尚未在交易所得到完整确认；继续核验，禁止重复提交")

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
                and algo_matches(item, attempt, expected_protection(self.ledger, self.gateway, attempt), filled)
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
