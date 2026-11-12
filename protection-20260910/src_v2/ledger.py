from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from src_v2.parser import TradeSignal


@dataclass(frozen=True)
class StrategyRecord:
    strategy_id: str
    symbol: str
    state: str
    source_chat_id: int
    source_sender_id: int
    source_message_id: int


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    strategy_id: str
    symbol: str
    direction: str
    state: str
    cl_ord_id: str
    ord_id: str | None
    requested_size: str | None
    filled_size: str
    retry_count: int
    updated_at: str


class LedgerConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ProtectionRecord:
    attempt_id: str
    algo_id: str
    covered_size: str
    state: str


@dataclass(frozen=True)
class NotificationRecord:
    notification_id: int
    dedupe_key: str
    kind: str
    message: str
    state: str
    attempt_count: int
    last_error: str | None
    updated_at: str


@dataclass(frozen=True)
class IngestEventRecord:
    event_id: int
    chat_id: int
    sender_id: int
    message_id: int
    event_kind: str
    raw_text: str
    decision_kind: str
    reasons: str
    created_at: str


@dataclass(frozen=True)
class OrderPlanSnapshot:
    attempt_id: str
    available_usdt: str
    leverage: int
    price: str
    size: str
    margin_fraction: str
    created_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attempt_id(strategy_id: str, direction: str) -> str:
    return f"{strategy_id}:{direction}"


def _client_order_id(strategy_id: str, direction: str, retry_count: int = 0) -> str:
    identity = f"{_attempt_id(strategy_id, direction)}:{retry_count}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return f"BZ{digest}{'L' if direction == 'long' else 'S'}"


class Ledger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._initialize()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2, 3, 4}:
                raise RuntimeError(f"不支持的账本 schema 版本: {version}")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategy_versions (
                    strategy_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    state TEXT NOT NULL,
                    source_chat_id INTEGER NOT NULL,
                    source_sender_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    raw_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_chat_id, source_message_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_strategy_per_symbol
                    ON strategy_versions(symbol)
                    WHERE state = 'ACTIVE';

                CREATE TABLE IF NOT EXISTS direction_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL REFERENCES strategy_versions(strategy_id),
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('long', 'short')),
                    state TEXT NOT NULL,
                    cl_ord_id TEXT NOT NULL UNIQUE,
                    ord_id TEXT,
                    requested_size TEXT,
                    filled_size TEXT NOT NULL DEFAULT '0',
                    entry_low TEXT,
                    entry_high TEXT,
                    entry_price TEXT,
                    stop_price TEXT,
                    target_price TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(strategy_id, direction)
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT,
                    attempt_id TEXT,
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS protections (
                    attempt_id TEXT PRIMARY KEY REFERENCES direction_attempts(attempt_id),
                    algo_id TEXT NOT NULL UNIQUE,
                    covered_size TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ingest_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    event_kind TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    decision_kind TEXT NOT NULL,
                    reasons TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id, event_kind)
                );

                CREATE TABLE IF NOT EXISTS runtime_controls (
                    control_key TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_jobs (
                    strategy_id TEXT PRIMARY KEY REFERENCES strategy_versions(strategy_id),
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    deadline TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_plan_snapshots (
                    attempt_id TEXT PRIMARY KEY REFERENCES direction_attempts(attempt_id),
                    available_usdt TEXT NOT NULL,
                    leverage INTEGER NOT NULL,
                    price TEXT NOT NULL,
                    size TEXT NOT NULL,
                    margin_fraction TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            required_columns = {
                "strategy_versions": {"strategy_id", "symbol", "state", "raw_text"},
                "direction_attempts": {
                    "attempt_id",
                    "strategy_id",
                    "state",
                    "cl_ord_id",
                    "retry_count",
                    "filled_size",
                },
                "protections": {"attempt_id", "algo_id", "covered_size", "state"},
                "notifications": {
                    "notification_id",
                    "dedupe_key",
                    "state",
                    "attempt_count",
                },
                "runtime_controls": {
                    "control_key",
                    "state",
                    "reason",
                    "updated_at",
                },
                "order_plan_snapshots": {
                    "attempt_id",
                    "available_usdt",
                    "leverage",
                    "price",
                    "size",
                    "margin_fraction",
                    "created_at",
                },
            }
            for table, required in required_columns.items():
                actual = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if not required.issubset(actual):
                    raise RuntimeError(f"账本表 {table} 与当前 schema 不兼容")
            # additive migration：补齐旧 direction_attempts 表缺失的信号参数列
            signal_columns = {
                "entry_low": "TEXT",
                "entry_high": "TEXT",
                "entry_price": "TEXT",
                "stop_price": "TEXT",
                "target_price": "TEXT",
            }
            actual_attempt_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(direction_attempts)"
                )
            }
            for column, column_type in signal_columns.items():
                if column not in actual_attempt_columns:
                    connection.execute(
                        f"ALTER TABLE direction_attempts "
                        f"ADD COLUMN {column} {column_type}"
                    )
            if version in {0, 1, 2, 3}:
                connection.execute("PRAGMA user_version=4")
            connection.commit()

    def record_strategy(
        self,
        *,
        strategy_id: str,
        symbol: str,
        source_chat_id: int,
        source_sender_id: int,
        source_message_id: int,
        raw_text: str,
        directions: tuple[str, ...],
        signals: tuple[TradeSignal, ...] = (),
        execution_deadline: str | None = None,
    ) -> StrategyRecord:
        if not directions or len(set(directions)) != len(directions):
            raise ValueError("每个策略必须有且只有一个每方向订单")
        if any(direction not in {"long", "short"} for direction in directions):
            raise ValueError("方向必须是 long 或 short")
        now = _utc_now()
        signal_by_direction = {signal.direction: signal for signal in signals}
        if signals and set(signal_by_direction) != set(directions):
            raise ValueError("信号和方向列表不一致")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM strategy_versions WHERE strategy_id = ?", (strategy_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    UPDATE strategy_versions
                       SET state='SUPERSEDED', updated_at=?
                     WHERE symbol=? AND state='ACTIVE'
                    """,
                    (now, symbol),
                )
                connection.execute(
                    """
                    INSERT INTO strategy_versions(
                        strategy_id, symbol, state, source_chat_id,
                        source_sender_id, source_message_id, raw_text,
                        created_at, updated_at
                    ) VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        strategy_id,
                        symbol,
                        source_chat_id,
                        source_sender_id,
                        source_message_id,
                        raw_text,
                        now,
                        now,
                    ),
                )
                for direction in directions:
                    attempt_id = _attempt_id(strategy_id, direction)
                    connection.execute(
                        """
                        INSERT INTO direction_attempts(
                            attempt_id, strategy_id, symbol, direction, state,
                            cl_ord_id, entry_low, entry_high, entry_price,
                            stop_price, target_price, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'READY', ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            strategy_id,
                            symbol,
                            direction,
                            _client_order_id(strategy_id, direction),
                            str(signal_by_direction[direction].entry_low)
                            if direction in signal_by_direction
                            else None,
                            str(signal_by_direction[direction].entry_high)
                            if direction in signal_by_direction
                            else None,
                            str(signal_by_direction[direction].entry_price)
                            if direction in signal_by_direction
                            else None,
                            str(signal_by_direction[direction].stop)
                            if direction in signal_by_direction
                            else None,
                            str(signal_by_direction[direction].target)
                            if direction in signal_by_direction
                            else None,
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO audit_events(strategy_id, event_type, detail, created_at)
                    VALUES (?, 'STRATEGY_RECORDED', ?, ?)
                    """,
                    (strategy_id, symbol, now),
                )
                if execution_deadline is not None:
                    # Commit source proof and its work item with the strategy.
                    # A crash can leave neither, or all three, never an orphan READY.
                    connection.execute(
                        "INSERT OR IGNORE INTO ingest_events(chat_id,sender_id,message_id,event_kind,raw_text,decision_kind,reasons,created_at) VALUES (?,?,?,'new',?,'READY','',?)",
                        (source_chat_id,source_sender_id,source_message_id,raw_text,now),
                    )
                    connection.execute(
                        "INSERT INTO execution_jobs(strategy_id,state,next_attempt_at,deadline,updated_at) VALUES (?,'PENDING',?,?,?)",
                        (strategy_id,now,execution_deadline,now),
                    )
            connection.commit()
        strategy = self.get_strategy(strategy_id)
        if strategy is None:
            raise RuntimeError("策略写入后无法读回")
        return strategy

    def get_execution_job(self, strategy_id: str) -> dict | None:
        with self._connection() as c:
            row = c.execute("SELECT * FROM execution_jobs WHERE strategy_id=?", (strategy_id,)).fetchone()
        return dict(row) if row else None

    def due_execution_jobs(self, now: str, limit: int = 20) -> list[dict]:
        with self._connection() as c:
            rows = c.execute(
                "SELECT * FROM execution_jobs WHERE state IN ('PENDING','RETRY','RUNNING','RECONCILING') AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?",
                (now, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_execution_job(self, strategy_id: str, *, state: str, now: str,
                             next_attempt_at: str | None = None, error: str = '', started: bool = False,
                             notification: dict | None = None) -> None:
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE execution_jobs SET state=?,attempts=attempts+?,next_attempt_at=?,last_error=?,updated_at=? WHERE strategy_id=?",
                      (state,int(started),next_attempt_at or now,error,now,strategy_id))
            if notification is not None:
                c.execute("INSERT OR IGNORE INTO notifications(dedupe_key,kind,message,state,attempt_count,created_at,updated_at) VALUES (?,?,?,'PENDING',0,?,?)",
                          (notification['dedupe_key'],notification['kind'],notification['message'],now,now))
            c.commit()

    def adopt_verified_live_order(
        self,
        *,
        strategy_id: str,
        source_chat_id: int,
        source_sender_id: int,
        source_message_id: int,
        raw_text: str,
        signal: TradeSignal,
        cl_ord_id: str,
        ord_id: str,
        requested_size: str,
    ) -> AttemptRecord:
        """Atomically register an externally-created, freshly verified live order.

        Exchange validation belongs to the handoff layer.  This method only
        commits the already-verified identity and signal to the authoritative
        ledger so later replacement and reconciliation remain ownership-safe.
        """
        if not strategy_id or not cl_ord_id or not ord_id or not requested_size:
            raise ValueError("接管订单的身份字段不能为空")
        now = _utc_now()
        attempt_id = _attempt_id(strategy_id, signal.direction)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM direction_attempts WHERE attempt_id=? OR cl_ord_id=? OR ord_id=?",
                (attempt_id, cl_ord_id, ord_id),
            ).fetchall()
            if existing:
                if len(existing) == 1:
                    row = existing[0]
                    exact = (
                        row["attempt_id"] == attempt_id
                        and row["strategy_id"] == strategy_id
                        and row["symbol"] == signal.symbol
                        and row["direction"] == signal.direction
                        and row["state"] == "LIVE"
                        and row["cl_ord_id"] == cl_ord_id
                        and row["ord_id"] == ord_id
                        and row["requested_size"] == requested_size
                        and str(row["filled_size"] or "0") in {"0", "0.0", ""}
                    )
                    if exact:
                        connection.commit()
                        return self._attempt_from_row(row)
                raise LedgerConflict("接管订单与已有账本身份冲突")
            try:
                connection.execute(
                    """
                    UPDATE strategy_versions
                       SET state='SUPERSEDED', updated_at=?
                     WHERE symbol=? AND state='ACTIVE'
                    """,
                    (now, signal.symbol),
                )
                connection.execute(
                    """
                    INSERT INTO strategy_versions(
                        strategy_id, symbol, state, source_chat_id,
                        source_sender_id, source_message_id, raw_text,
                        created_at, updated_at
                    ) VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        strategy_id,
                        signal.symbol,
                        source_chat_id,
                        source_sender_id,
                        source_message_id,
                        raw_text,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO direction_attempts(
                        attempt_id, strategy_id, symbol, direction, state,
                        cl_ord_id, ord_id, requested_size, filled_size,
                        entry_low, entry_high, entry_price, stop_price,
                        target_price, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'LIVE', ?, ?, ?, '0', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        strategy_id,
                        signal.symbol,
                        signal.direction,
                        cl_ord_id,
                        ord_id,
                        requested_size,
                        str(signal.entry_low),
                        str(signal.entry_high),
                        str(signal.entry_price),
                        str(signal.stop),
                        str(signal.target),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO audit_events(
                        strategy_id, attempt_id, event_type, detail, created_at
                    ) VALUES (?, ?, 'LEGACY_ORDER_ADOPTED', ?, ?)
                    """,
                    (strategy_id, attempt_id, f"ord_id={ord_id};zero_fill=true", now),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerConflict("接管订单写入违反账本唯一性") from exc
            connection.commit()
        adopted = self.get_attempt(attempt_id)
        if adopted is None:
            raise RuntimeError("接管订单写入后无法读回")
        return adopted

    def get_strategy(self, strategy_id: str) -> StrategyRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_versions WHERE strategy_id=?", (strategy_id,)
            ).fetchone()
        return self._strategy_from_row(row) if row else None

    def get_strategy_by_source(self, chat_id: int, message_id: int) -> StrategyRecord | None:
        """Resolve a Telegram deletion by source identity, including adopted records."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_versions WHERE source_chat_id=? AND source_message_id=?",
                (chat_id, message_id),
            ).fetchone()
        return self._strategy_from_row(row) if row else None

    def get_active_strategy(self, symbol: str) -> StrategyRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_versions WHERE symbol=? AND state='ACTIVE'",
                (symbol,),
            ).fetchone()
        return self._strategy_from_row(row) if row else None

    def strategy_has_authenticated_ready_ingest(self, strategy_id: str) -> bool:
        """Prove that a strategy came from an authenticated, complete new event."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1
                  FROM strategy_versions AS s
                  JOIN ingest_events AS i
                    ON i.chat_id=s.source_chat_id
                   AND i.sender_id=s.source_sender_id
                   AND i.message_id=s.source_message_id
                   AND i.raw_text=s.raw_text
                 WHERE s.strategy_id=?
                   AND i.event_kind='new'
                   AND i.decision_kind='READY'
                 LIMIT 1
                """,
                (strategy_id,),
            ).fetchone()
        return row is not None

    def list_attempts(self, strategy_id: str) -> tuple[AttemptRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM direction_attempts WHERE strategy_id=?
                ORDER BY CASE direction WHEN 'long' THEN 0 ELSE 1 END
                """,
                (strategy_id,),
            ).fetchall()
        return tuple(self._attempt_from_row(row) for row in rows)

    def list_reconcilable_attempts(self) -> tuple[AttemptRecord, ...]:
        """Return attempts whose exchange state can still change or needs recovery."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM direction_attempts
                 WHERE state IN (
                    'SUBMITTING', 'SUBMITTED', 'UNKNOWN',
                    'LIVE', 'PARTIALLY_FILLED', 'FILLED',
                    'CANCELING', 'CANCEL_UNKNOWN', 'CANCELED_PARTIAL',
                    'PROTECTION_FAILED'
                 )
                 ORDER BY created_at, attempt_id
                """
            ).fetchall()
        return tuple(self._attempt_from_row(row) for row in rows)

    def get_attempt(self, attempt_id: str) -> AttemptRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM direction_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        return self._attempt_from_row(row) if row else None

    def get_attempt_by_cl_ord_id(self, cl_ord_id: str) -> AttemptRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM direction_attempts WHERE cl_ord_id=?", (cl_ord_id,)
            ).fetchone()
        return self._attempt_from_row(row) if row else None

    def list_superseded_unresolved_attempts(
        self, replacing_strategy_id: str
    ) -> tuple[AttemptRecord, ...]:
        strategy = self.get_strategy(replacing_strategy_id)
        if strategy is None:
            return ()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT a.*
                  FROM direction_attempts AS a
                  JOIN strategy_versions AS s ON s.strategy_id=a.strategy_id
                 WHERE a.symbol=?
                   AND a.strategy_id<>?
                   AND s.state='SUPERSEDED'
                   AND a.state IN (
                       'SUBMITTING', 'SUBMITTED', 'UNKNOWN',
                       'CANCELING', 'CANCEL_UNKNOWN',
                       'PARTIALLY_FILLED', 'FILLED',
                       'CANCELED_PARTIAL', 'PROTECTION_FAILED'
                   )
                 ORDER BY a.created_at
                """,
                (strategy.symbol, replacing_strategy_id),
            ).fetchall()
        return tuple(self._attempt_from_row(row) for row in rows)

    def get_attempt_signal(self, attempt_id: str) -> TradeSignal | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT symbol, direction, entry_low, entry_high, entry_price,
                       stop_price, target_price
                  FROM direction_attempts WHERE attempt_id=?
                """,
                (attempt_id,),
            ).fetchone()
        if row is None or row["entry_price"] is None:
            return None
        return TradeSignal(
            symbol=row["symbol"],
            direction=row["direction"],
            entry_low=float(row["entry_low"]),
            entry_high=float(row["entry_high"]),
            entry_price=float(row["entry_price"]),
            stop=float(row["stop_price"]),
            target=float(row["target_price"]),
        )

    def record_order_plan_snapshot(
        self,
        attempt_id: str,
        *,
        available_usdt: str,
        leverage: int,
        price: str,
        size: str,
        margin_fraction: str,
    ) -> OrderPlanSnapshot:
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO order_plan_snapshots(
                    attempt_id, available_usdt, leverage, price,
                    size, margin_fraction, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    available_usdt=excluded.available_usdt,
                    leverage=excluded.leverage,
                    price=excluded.price,
                    size=excluded.size,
                    margin_fraction=excluded.margin_fraction,
                    created_at=excluded.created_at
                """,
                (
                    attempt_id,
                    available_usdt,
                    leverage,
                    price,
                    size,
                    margin_fraction,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events(attempt_id, event_type, detail, created_at)
                VALUES (?, 'ORDER_PLAN_RECORDED', ?, ?)
                """,
                (
                    attempt_id,
                    f"margin_fraction={margin_fraction};leverage={leverage};size={size}",
                    now,
                ),
            )
            connection.commit()
        snapshot = self.get_order_plan_snapshot(attempt_id)
        if snapshot is None:
            raise RuntimeError("订单计划快照写入后无法读回")
        return snapshot

    def get_order_plan_snapshot(self, attempt_id: str) -> OrderPlanSnapshot | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM order_plan_snapshots WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None
        return OrderPlanSnapshot(
            attempt_id=row["attempt_id"],
            available_usdt=row["available_usdt"],
            leverage=row["leverage"],
            price=row["price"],
            size=row["size"],
            margin_fraction=row["margin_fraction"],
            created_at=row["created_at"],
        )

    def transition_attempt(
        self,
        attempt_id: str,
        *,
        expected_states: tuple[str, ...],
        new_state: str,
        ord_id: str | None = None,
        requested_size: str | None = None,
        filled_size: str | None = None,
        error: str | None = None,
    ) -> AttemptRecord:
        if not expected_states:
            raise ValueError("必须声明允许的原状态")
        now = _utc_now()
        placeholders = ",".join("?" for _ in expected_states)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE direction_attempts
                   SET state=?,
                       ord_id=COALESCE(?, ord_id),
                       requested_size=COALESCE(?, requested_size),
                       filled_size=COALESCE(?, filled_size),
                       last_error=?,
                       updated_at=?
                 WHERE attempt_id=? AND state IN ({placeholders})
                """,
                (
                    new_state,
                    ord_id,
                    requested_size,
                    filled_size,
                    error,
                    now,
                    attempt_id,
                    *expected_states,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise LedgerConflict(
                    f"订单尝试 {attempt_id} 状态已变化，拒绝覆盖"
                )
            connection.execute(
                """
                INSERT INTO audit_events(attempt_id, event_type, detail, created_at)
                VALUES (?, 'ATTEMPT_TRANSITION', ?, ?)
                """,
                (attempt_id, new_state, now),
            )
            connection.commit()
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise RuntimeError("订单状态写入后无法读回")
        return attempt

    def prepare_confirmed_missing_retry(self, attempt_id: str) -> AttemptRecord:
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT strategy_id, direction, retry_count
                  FROM direction_attempts
                 WHERE attempt_id=? AND state='MISSING_CONFIRMED'
                """,
                (attempt_id,),
            ).fetchone()
            if row is None or row["retry_count"] >= 3:
                connection.rollback()
                raise LedgerConflict("只允许对已确认缺失的订单重试三次")
            retry_count = row["retry_count"] + 1
            cl_ord_id = _client_order_id(
                row["strategy_id"], row["direction"], retry_count
            )
            connection.execute(
                """
                UPDATE direction_attempts
                   SET state='READY', retry_count=?, cl_ord_id=?, ord_id=NULL,
                       requested_size=NULL, last_error=NULL, updated_at=?
                 WHERE attempt_id=?
                """,
                (retry_count, cl_ord_id, now, attempt_id),
            )
            connection.execute(
                """
                INSERT INTO audit_events(attempt_id, event_type, detail, created_at)
                VALUES (?, 'RETRY_AUTHORIZED', ?, ?)
                """,
                (attempt_id, str(retry_count), now),
            )
            connection.commit()
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise RuntimeError("重试状态写入后无法读回")
        return attempt

    def record_protection(
        self, attempt_id: str, *, algo_id: str, covered_size: str, state: str
    ) -> ProtectionRecord:
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO protections(
                    attempt_id, algo_id, covered_size, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    algo_id=excluded.algo_id,
                    covered_size=excluded.covered_size,
                    state=excluded.state,
                    updated_at=excluded.updated_at
                """,
                (attempt_id, algo_id, covered_size, state, now, now),
            )
            connection.commit()
        protection = self.get_protection(attempt_id)
        if protection is None:
            raise RuntimeError("保护单写入后无法读回")
        return protection

    def record_attachment_intent(self, attempt_id: str, payload: dict) -> None:
        attempt = self.get_attempt(attempt_id)
        if attempt is None or payload.get("attachAlgoClOrdId") != attempt.cl_ord_id + "P":
            raise ValueError("Attachment identity mismatch")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO audit_events(attempt_id,event_type,detail,created_at) VALUES(?,?,?,?)",
                (attempt_id, "NATIVE_TPSL_INTENT", json.dumps({"clOrdId": attempt.cl_ord_id, "payload": payload}), _utc_now()),
            )

            connection.commit()

    def get_attachment_intent(self, attempt_id: str) -> dict | None:
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            return None
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT detail FROM audit_events WHERE attempt_id=? AND event_type='NATIVE_TPSL_INTENT' ORDER BY event_id DESC",
                (attempt_id,),
            ).fetchall()
        for row in rows:
            detail = json.loads(row["detail"])
            if detail["clOrdId"] == attempt.cl_ord_id:
                return detail["payload"]
        return None

    def get_protection(self, attempt_id: str) -> ProtectionRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM protections WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        if row is None:
            return None
        return ProtectionRecord(
            attempt_id=row["attempt_id"],
            algo_id=row["algo_id"],
            covered_size=row["covered_size"],
            state=row["state"],
        )

    def enqueue_notification(
        self, *, dedupe_key: str, kind: str, message: str
    ) -> NotificationRecord:
        if not dedupe_key or not kind or not message:
            raise ValueError("通知去重键、类型和内容不能为空")
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO notifications(
                    dedupe_key, kind, message, state, created_at, updated_at
                ) VALUES (?, ?, ?, 'PENDING', ?, ?)
                """,
                (dedupe_key, kind, message, now, now),
            )
            row = connection.execute(
                "SELECT * FROM notifications WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("通知写入后无法读回")
        return self._notification_from_row(row)

    def list_pending_notifications(
        self, *, limit: int = 100
    ) -> tuple[NotificationRecord, ...]:
        if limit <= 0:
            return ()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notifications
                 WHERE state='PENDING'
                 ORDER BY notification_id
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._notification_from_row(row) for row in rows)

    def has_failed_notification_backlog(self, *, threshold: int = 3) -> bool:
        if threshold <= 0:
            raise ValueError("通知失败阈值必须大于 0")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM notifications
                 WHERE state='PENDING' AND attempt_count>=?
                 LIMIT 1
                """,
                (threshold,),
            ).fetchone()
        return row is not None

    def mark_notification_sent(self, notification_id: int) -> None:
        now = _utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE notifications
                   SET state='SENT', last_error=NULL, updated_at=?
                 WHERE notification_id=? AND state='PENDING'
                """,
                (now, notification_id),
            )
            if cursor.rowcount != 1:
                raise LedgerConflict("通知状态已变化")
            connection.commit()

    def mark_notification_failed(self, notification_id: int, error: str) -> None:
        now = _utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE notifications
                   SET attempt_count=attempt_count+1,
                       last_error=?, updated_at=?
                 WHERE notification_id=? AND state='PENDING'
                """,
                (error[:500], now, notification_id),
            )
            if cursor.rowcount != 1:
                raise LedgerConflict("通知状态已变化")
            connection.commit()

    def record_ingest_event(
        self,
        *,
        chat_id: int,
        sender_id: int,
        message_id: int,
        event_kind: str,
        raw_text: str,
        decision_kind: str,
        reasons: str,
    ) -> IngestEventRecord:
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO ingest_events(
                    chat_id, sender_id, message_id, event_kind,
                    raw_text, decision_kind, reasons, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    sender_id,
                    message_id,
                    event_kind,
                    raw_text,
                    decision_kind,
                    reasons,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM ingest_events "
                "WHERE chat_id=? AND message_id=? AND event_kind=?",
                (chat_id, message_id, event_kind),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("intake 审计写入后无法读回")
        return self._ingest_event_from_row(row)

    def list_ingest_events(
        self, *, limit: int = 100
    ) -> tuple[IngestEventRecord, ...]:
        if limit <= 0:
            return ()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM ingest_events ORDER BY event_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._ingest_event_from_row(row) for row in rows)

    def get_ingest_event(
        self,
        *,
        chat_id: int,
        sender_id: int,
        message_id: int,
        event_kind: str = "new",
    ) -> IngestEventRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM ingest_events
                 WHERE chat_id=? AND sender_id=? AND message_id=? AND event_kind=?
                """,
                (chat_id, sender_id, message_id, event_kind),
            ).fetchone()
        return self._ingest_event_from_row(row) if row else None

    def authorize_ingest_reclassification(
        self,
        *,
        chat_id: int,
        sender_id: int,
        message_id: int,
        raw_text: str,
        reason: str,
    ) -> IngestEventRecord:
        """Promote one exact authenticated historical event after user approval.

        The source identity and original text must match byte-for-byte.  This is
        deliberately narrower than accepting a new manual strategy and leaves a
        durable audit event.  Repeating the exact operation is idempotent.
        """
        if not reason.strip():
            raise ValueError("重分类原因不能为空")
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM ingest_events
                 WHERE chat_id=? AND sender_id=? AND message_id=?
                   AND event_kind='new'
                """,
                (chat_id, sender_id, message_id),
            ).fetchone()
            if row is None or row["raw_text"] != raw_text:
                raise LedgerConflict("已认证消息身份或原文不匹配")
            if row["decision_kind"] != "READY":
                connection.execute(
                    """
                    UPDATE ingest_events
                       SET decision_kind='READY', reasons=?
                     WHERE event_id=?
                    """,
                    (reason, row["event_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO audit_events(event_type, detail, created_at)
                    VALUES ('USER_APPROVED_RECLASSIFICATION', ?, ?)
                    """,
                    (f"message_id={message_id}; {reason}", now),
                )
                notification = connection.execute(
                    """
                    UPDATE notifications
                       SET state='SUPPRESSED', last_error=NULL, updated_at=?
                     WHERE dedupe_key=? AND state='PENDING'
                    """,
                    (now, f"review:{chat_id}:{message_id}:new"),
                )
                if notification.rowcount:
                    connection.execute(
                        """
                        INSERT INTO audit_events(event_type, detail, created_at)
                        VALUES ('OBSOLETE_REVIEW_NOTIFICATION_SUPPRESSED', ?, ?)
                        """,
                        (f"message_id={message_id}; parser_reclassified=READY", now),
                    )
            updated = connection.execute(
                "SELECT * FROM ingest_events WHERE event_id=?",
                (row["event_id"],),
            ).fetchone()
            connection.commit()
        if updated is None:
            raise RuntimeError("重分类后无法读回消息审计")
        return self._ingest_event_from_row(updated)

    def pause_automation(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("暂停原因不能为空")
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT state, reason FROM runtime_controls "
                "WHERE control_key='automation'"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO runtime_controls(control_key, state, reason, updated_at)
                VALUES ('automation', 'PAUSED', ?, ?)
                ON CONFLICT(control_key) DO UPDATE SET
                    state='PAUSED', reason=excluded.reason, updated_at=excluded.updated_at
                """,
                (reason, now),
            )
            if existing is None or existing["state"] != "PAUSED" or existing["reason"] != reason:
                connection.execute(
                    """
                    INSERT INTO audit_events(event_type, detail, created_at)
                    VALUES ('AUTOMATION_PAUSED', ?, ?)
                    """,
                    (reason, now),
                )
            connection.commit()

    def bind_account(self, fingerprint: str, *, reason: str) -> None:
        normalized = fingerprint.strip().lower()
        if len(normalized) != 64:
            raise ValueError("OKX 账号指纹必须是完整 SHA-256")
        try:
            int(normalized, 16)
        except ValueError as exc:
            raise ValueError("OKX 账号指纹格式无效") from exc
        if not reason.strip():
            raise ValueError("账号绑定原因不能为空")
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT state FROM runtime_controls "
                "WHERE control_key='okx_account'"
            ).fetchone()
            if existing is not None:
                if existing["state"] != normalized:
                    raise LedgerConflict("账本已经绑定其他 OKX 账号")
                connection.commit()
                return
            connection.execute(
                """
                INSERT INTO runtime_controls(control_key, state, reason, updated_at)
                VALUES ('okx_account', ?, ?, ?)
                """,
                (normalized, reason, now),
            )
            connection.execute(
                """
                INSERT INTO audit_events(event_type, detail, created_at)
                VALUES ('OKX_ACCOUNT_BOUND', ?, ?)
                """,
                (f"fingerprint={normalized[:12]}", now),
            )
            connection.commit()

    def account_fingerprint(self) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state FROM runtime_controls "
                "WHERE control_key='okx_account'"
            ).fetchone()
        return str(row["state"]) if row is not None else None

    def automation_pause_reason(self) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state, reason FROM runtime_controls "
                "WHERE control_key='automation'"
            ).fetchone()
        if row is None or row["state"] != "PAUSED":
            return None
        return str(row["reason"])

    def resume_automation(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("恢复原因不能为空")
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runtime_controls(control_key, state, reason, updated_at)
                VALUES ('automation', 'RUNNING', ?, ?)
                ON CONFLICT(control_key) DO UPDATE SET
                    state='RUNNING', reason=excluded.reason, updated_at=excluded.updated_at
                """,
                (reason, now),
            )
            connection.execute(
                """
                INSERT INTO audit_events(event_type, detail, created_at)
                VALUES ('AUTOMATION_RESUMED', ?, ?)
                """,
                (reason, now),
            )
            connection.commit()

    @staticmethod
    def _ingest_event_from_row(row: sqlite3.Row) -> IngestEventRecord:
        return IngestEventRecord(
            event_id=row["event_id"],
            chat_id=row["chat_id"],
            sender_id=row["sender_id"],
            message_id=row["message_id"],
            event_kind=row["event_kind"],
            raw_text=row["raw_text"],
            decision_kind=row["decision_kind"],
            reasons=row["reasons"],
            created_at=row["created_at"],
        )

    def is_cancellable_old_entry(
        self, order: dict, *, replacing_strategy_id: str
    ) -> bool:
        if (
            order.get("tag") != "BANGZHU"
            or not str(order.get("clOrdId") or "").startswith("BZ")
            or order.get("ordType") != "limit"
            or order.get("state") != "live"
            or not order.get("ordId")
        ):
            return False
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT a.ord_id, a.symbol, a.state, s.state AS strategy_state
                  FROM direction_attempts AS a
                  JOIN strategy_versions AS s ON s.strategy_id = a.strategy_id
                 WHERE a.cl_ord_id=?
                   AND a.strategy_id<>?
                """,
                (order.get("clOrdId"), replacing_strategy_id),
            ).fetchone()
        return bool(
            row
            and row["ord_id"] == order.get("ordId")
            and row["symbol"] == order.get("instId")
            and row["state"] == "LIVE"
            and row["strategy_state"] == "SUPERSEDED"
        )

    @staticmethod
    def _strategy_from_row(row: sqlite3.Row) -> StrategyRecord:
        return StrategyRecord(
            strategy_id=row["strategy_id"],
            symbol=row["symbol"],
            state=row["state"],
            source_chat_id=row["source_chat_id"],
            source_sender_id=row["source_sender_id"],
            source_message_id=row["source_message_id"],
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> AttemptRecord:
        return AttemptRecord(
            attempt_id=row["attempt_id"],
            strategy_id=row["strategy_id"],
            symbol=row["symbol"],
            direction=row["direction"],
            state=row["state"],
            cl_ord_id=row["cl_ord_id"],
            ord_id=row["ord_id"],
            requested_size=row["requested_size"],
            filled_size=row["filled_size"],
            retry_count=row["retry_count"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> NotificationRecord:
        return NotificationRecord(
            notification_id=row["notification_id"],
            dedupe_key=row["dedupe_key"],
            kind=row["kind"],
            message=row["message"],
            state=row["state"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )
