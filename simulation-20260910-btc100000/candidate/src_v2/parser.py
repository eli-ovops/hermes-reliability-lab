from dataclasses import dataclass
from enum import Enum
from decimal import Decimal
import re


class DecisionKind(str, Enum):
    IGNORE = "IGNORE"
    READY = "READY"
    READY_WITH_REVIEW = "READY_WITH_REVIEW"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True)
class ParseDecision:
    kind: DecisionKind
    signals: tuple = ()
    questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradeSignal:
    symbol: str
    direction: str
    entry_low: float
    entry_high: float
    entry_price: float
    stop: float
    target: float


PRICE = r"\d{1,8}(?:\.\d+)?"
RANGE = rf"(?P<p1>{PRICE})(?:\s*[-~至到]\s*(?P<p2>{PRICE}))?"
# Reject numbered-list markers such as ``1：`` / ``2、`` as entry prices.
# Without this guard, a nearby real range (for example 2385-2400) can be
# swallowed by the wildcard before the direction word while p1 incorrectly
# becomes the list number.
ACTION_RANGE = rf"{RANGE}(?!\s*[:：、.])"
COINS = (
    (r"比特币|\bBTC\b", "BTC-USDT-SWAP"),
    (r"以太坊|\bETH\b", "ETH-USDT-SWAP"),
    (r"索拉纳|\bSOL\b", "SOL-USDT-SWAP"),
    (r"\bHYPE\b", "HYPE-USDT-SWAP"),
    (r"\bUNI\b", "UNI-USDT-SWAP"),
    (r"\bPEPE\b", "PEPE-USDT-SWAP"),
    (r"\bONDO\b", "ONDO-USDT-SWAP"),
    (r"\bAAVE\b", "AAVE-USDT-SWAP"),
    (r"\bLINK\b", "LINK-USDT-SWAP"),
    (r"\bSUI\b", "SUI-USDT-SWAP"),
    (r"\bBCH\b", "BCH-USDT-SWAP"),
    (r"\bZEC\b", "ZEC-USDT-SWAP"),
    (r"\bENA\b", "ENA-USDT-SWAP"),
    (r"\bARB\b", "ARB-USDT-SWAP"),
)
DIRECTIONS = (
    (
        "long",
        r"逢低做多|逢低接多|回踩买入|轻仓试多|做多|接多|低多|买入|低吸|试多|买点",
    ),
    ("short", r"反弹做空|做空|试空|轻仓试空|高抛|卖出"),
)
NEGATED_ENTRY = re.compile(
    r"(?:不要|别|不要再|暂停|取消|不建议|不考虑|暂不|不再|避免)"
    r"(?:立即|继续|轻仓)?(?:做多|做空|买入|卖出|试多|试空|下单|挂单|低吸|买点|进场|进多|进)"
)
CONDITIONAL_TRIGGER = re.compile(
    r"(?:如果|若|撑住后|站稳后|突破后|跌破后|确认后|企稳后|回踩后)"
    r"[^。；\n]{0,20}"
    r"(?:逢低做多|逢低接多|回踩买入|轻仓试多|做多|做空|买入|卖出|低吸|试多|试空|买点|进场|进多|接多|低多|高抛|进)"
)
WAITING_WORDS = re.compile(
    r"观察(?!到)|观望|先看|先观望|静待|再看|再看看"
)
WAITING_IN_TRAILING = re.compile(r"等待|等确认|待确认")
WATCH_ACTION = re.compile(
    r"关注[^。；\n]{0,12}"
    r"(?:逢低做多|逢低接多|回踩买入|轻仓试多|做多|做空|买入|卖出|低吸|试多|试空|买点|进场|进多|接多|低多|高抛)"
)
CENTER_RANGE = re.compile(
    rf"(?P<p1>{PRICE})\s*[-~至到]\s*(?P<p2>{PRICE})"
    r"(?:附近)?[^。；\n]{0,24}?(?:区间(?:内)?震荡|震荡区间|中枢)"
)


def _matched_symbols(text: str) -> set[str]:
    return {
        symbol
        for pattern, symbol in COINS
        if re.search(pattern, text, re.IGNORECASE | re.ASCII)
    }


def _single_symbol(text: str) -> str | None:
    symbols = _matched_symbols(text)
    return next(iter(symbols)) if len(symbols) == 1 else None


def _implicit_btc_symbol(
    text: str,
    *,
    action_start: int,
    action_count: int,
    entry: float,
    stop: float,
    target: float,
) -> str | None:
    """Infer BTC only for one complete, ticker-less five-digit strategy."""
    if action_count != 1 or not all(50_000 <= price <= 99_999 for price in (entry, stop, target)):
        return None
    nearby = text[max(0, action_start - 24):action_start]
    if re.search(r"(?<![A-Z])[A-Z]{2,10}(?![A-Z])", nearby, re.ASCII):
        return None
    return "BTC-USDT-SWAP"


def _nearest(match: re.Match, entry: float) -> float:
    values = [float(match.group("p1"))]
    if match.groupdict().get("p2"):
        values.append(float(match.group("p2")))
    return min(values, key=lambda value: abs(value - entry))


def _normalize_price_units(text: str) -> str:
    return re.sub(
        r"(\d+(?:\.\d+)?)\s*万",
        lambda match: str(int(float(match.group(1)) * 10000)),
        text,
    )


def _normalize_compact_btc_entry(text: str) -> str:
    # Only the complete entry form explicitly requested by the user is accepted.
    # Prefixes/suffixes mentioning existing positions, cancellation, or conditions
    # cannot match and still pass through the original management guards.
    match = re.fullmatch(
        rf"\s*(?:BTC|比特币)\s*(?P<entry>{PRICE})\s*(?P<direction>空单|多单)"
        rf"[，,。；;\s]+(?P<stop>{PRICE})\s*止损"
        rf"[，,。；;\s]+(?P<target>{PRICE})\s*止盈[。.!！\s]*",
        text, re.IGNORECASE,
    )
    if match is None:
        return text
    direction = "做空" if match["direction"] == "空单" else "做多"
    return f"BTC {match['entry']}{direction}，止损{match['stop']}，止盈{match['target']}"


def analyze_message(text: str) -> ParseDecision:
    text = _normalize_compact_btc_entry(_normalize_price_units(text))
    if NEGATED_ENTRY.search(text):
        return ParseDecision(
            kind=DecisionKind.NEEDS_REVIEW,
            questions=("检测到否定或取消交易指令",),
        )
    if CONDITIONAL_TRIGGER.search(text):
        questions = ["检测到尚待触发或确认的条件策略"]
        if "止损" not in text:
            questions.append("缺少明确止损")
        if "止盈" not in text:
            questions.append("缺少明确止盈")
        return ParseDecision(
            kind=DecisionKind.NEEDS_REVIEW,
            questions=tuple(questions),
        )
    if WAITING_WORDS.search(text):
        return ParseDecision(
            kind=DecisionKind.NEEDS_REVIEW,
            questions=("检测到等待或观察语义，尚未确认入场时机",),
        )
    if WATCH_ACTION.search(text):
        return ParseDecision(
            kind=DecisionKind.NEEDS_REVIEW,
            questions=("检测到关注/观望入场点，尚未确认执行",),
        )
    matched_symbols = _matched_symbols(text)
    symbol = next(iter(matched_symbols)) if len(matched_symbols) == 1 else None
    actions = []
    for direction, direction_words in DIRECTIONS:
        for action in re.finditer(
            rf"{ACTION_RANGE}(?:区域|附近)?[^。；\n0-9]{{0,16}}(?:{direction_words})",
            text,
        ):
            actions.append((action.start(), direction, action))

    actions.sort(key=lambda item: item[0])
    if actions:
        action_directions = [direction for _, direction, _ in actions]
        if len(action_directions) != len(set(action_directions)):
            return ParseDecision(
                kind=DecisionKind.NEEDS_REVIEW,
                questions=("同一币种同一方向出现多个入场方案",),
            )
        if len(matched_symbols) > 1:
            return ParseDecision(
                kind=DecisionKind.NEEDS_REVIEW,
                questions=("多币种歧义，无法确定唯一交易标的",),
            )
        signals = []
        questions = []
        for index, (action_start, direction, action) in enumerate(actions):
            next_start = actions[index + 1][0] if index + 1 < len(actions) else len(text)
            entry_values = [float(action.group("p1"))]
            if action.groupdict().get("p2"):
                entry_values.append(float(action.group("p2")))
            entry_low, entry_high = min(entry_values), max(entry_values)
            entry = (entry_low + entry_high) / 2
            trailing = text[action.end():next_start]
            if WAITING_IN_TRAILING.search(trailing):
                questions.append(f"{symbol} {direction} 策略等待确认，尚未触发")
                continue
            stops, targets = [], []
            for mention in re.finditer(
                rf"(?P<label>止损|止盈|目标)\s*[:：,，]?\s*{RANGE}", trailing
            ):
                values = [float(mention.group(k)) for k in ("p1", "p2") if mention.groupdict().get(k)]
                (stops if mention.group("label") == "止损" else targets).extend(values)
            if not stops or not targets:
                questions.append(f"{symbol} {direction} 策略缺少明确止损或止盈")
                continue
            stop = min(stops, key=lambda p: abs(p - entry))
            target = min(targets, key=lambda p: abs(p - entry))
            valid_prices = (
                stop < entry < target
                if direction == "long"
                else target < entry < stop
            )
            if not valid_prices:
                questions.append(f"{direction} 策略价格方向矛盾")
                continue
            resolved_symbol = symbol or _implicit_btc_symbol(
                text,
                action_start=action_start,
                action_count=len(actions),
                entry=entry,
                stop=stop,
                target=target,
            )
            if not resolved_symbol:
                questions.append("缺少唯一明确币种")
                continue
            signals.append(
                TradeSignal(
                    symbol=resolved_symbol,
                    direction=direction,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    entry_price=entry,
                    stop=stop,
                    target=target,
                )
            )
        if signals and questions:
            return ParseDecision(
                kind=DecisionKind.READY_WITH_REVIEW,
                signals=tuple(signals),
                questions=tuple(questions),
            )
        if signals:
            return ParseDecision(kind=DecisionKind.READY, signals=tuple(signals))
        return ParseDecision(
            kind=DecisionKind.NEEDS_REVIEW,
            questions=tuple(questions) or ("未找到完整策略",),
        )

    center_range = CENTER_RANGE.search(text)
    if center_range:
        if not symbol:
            return ParseDecision(
                kind=DecisionKind.NEEDS_REVIEW,
                questions=("区间策略缺少唯一明确币种",),
            )
        first = float(center_range.group("p1"))
        second = float(center_range.group("p2"))
        low, high = min(first, second), max(first, second)
        if low == high:
            return ParseDecision(
                kind=DecisionKind.NEEDS_REVIEW,
                questions=("区间上下沿不能相同",),
            )
        return ParseDecision(
            kind=DecisionKind.READY,
            signals=(
                TradeSignal(
                    symbol=symbol,
                    direction="long",
                    entry_low=low,
                    entry_high=low,
                    entry_price=low,
                    stop=low * 0.98,
                    target=low * 1.02,
                ),
                TradeSignal(
                    symbol=symbol,
                    direction="short",
                    entry_low=high,
                    entry_high=high,
                    entry_price=high,
                    stop=high * 1.02,
                    target=high * 0.98,
                ),
            ),
        )

    direction_hit = any(
        re.search(words, text) for _, words in DIRECTIONS
    )
    if direction_hit:
        matched_symbols = {
            sym for pat, sym in COINS if re.search(pat, text, re.IGNORECASE | re.ASCII)
        }
        if len(matched_symbols) > 1:
            return ParseDecision(
                kind=DecisionKind.NEEDS_REVIEW,
                questions=("多币种歧义，无法确定唯一交易标的",),
            )
        return ParseDecision(
            kind=DecisionKind.NEEDS_REVIEW,
            questions=("缺少明确入场、止损和止盈",),
        )
    if "支撑" in text or "阻力" in text:
        return ParseDecision(
            kind=DecisionKind.NEEDS_REVIEW,
            questions=("只有支撑或阻力，缺少完整入场策略",),
        )
    if re.search(r"继续持有|止盈上移|移动止损|减仓|平仓|保本|锁利润|止盈|止损", text):
        return ParseDecision(
            kind=DecisionKind.NEEDS_REVIEW,
            questions=("检测到持仓管理内容，不作为新入场策略",),
        )
    return ParseDecision(kind=DecisionKind.IGNORE)


def analyze_approved_range(text: str, *, symbol: str, low: float, high: float) -> ParseDecision:
    """Pure conversion of an explicitly approved range, bound to the source text.

    Normal Telegram intake does not call this override. Its caller must record
    user confirmation alongside the exact authenticated source message.
    """
    text = _normalize_price_units(text)
    lower, upper = Decimal(str(low)), Decimal(str(high))
    if not lower.is_finite() or not upper.is_finite() or not 0 < lower < upper:
        raise ValueError("确认的区间必须是有效正数且下沿小于上沿")
    if _matched_symbols(text) != {symbol}:
        raise ValueError("确认币种与原文不一致或原文有歧义")
    ranges = {
        tuple(sorted((Decimal(m.group("p1")), Decimal(m.group("p2")))))
        for m in re.finditer(RANGE, text) if m.groupdict().get("p2")
    }
    if ranges != {(lower, upper)}:
        raise ValueError("确认区间与原文不一致或原文包含多个区间")
    return ParseDecision(
        kind=DecisionKind.READY,
        signals=tuple(
            TradeSignal(symbol, direction, float(entry), float(entry), float(entry),
                        float(entry * stop_factor), float(entry * target_factor))
            for direction, entry, stop_factor, target_factor in (
                ("long", lower, Decimal("0.98"), Decimal("1.02")),
                ("short", upper, Decimal("1.02"), Decimal("0.98")),
            )
        ),
    )
