"""
Data layer. Design notes (also in README):

- SQLite in WAL mode, manual transaction control (isolation_level=None).
- Every write path runs inside `BEGIN IMMEDIATE ... COMMIT`, which makes
  SQLite take the write lock up front. Combined with a process-level
  threading.Lock around the critical section, this means two concurrent
  requests for the same (or different) users cannot interleave a
  read-modify-write on user_stats. This is the "safe handling of
  simultaneous requests" requirement.
- Duplicate processing is prevented by a UNIQUE constraint on
  idempotency_key. If an insert collides, we don't error - we look up
  the original transaction and return it, untouched, marked as a replay.
  This means retries (network blips, double-clicks, client retries) are
  always safe to send.
- In production with multiple backend processes/instances, swap SQLite
  for Postgres and replace the threading.Lock with
  `SELECT ... FOR UPDATE` on the user_stats row (the BEGIN IMMEDIATE
  pattern maps directly to that).
"""
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ranking import compute_burst_penalty, update_anomaly_score

DB_PATH = Path(__file__).parent / "app.db"
_write_lock = threading.Lock()


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            is_replay INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id TEXT PRIMARY KEY,
            total_amount REAL NOT NULL DEFAULT 0,
            transaction_count INTEGER NOT NULL DEFAULT 0,
            first_seen_at REAL,
            last_seen_at REAL,
            last_active_date TEXT,
            active_days INTEGER NOT NULL DEFAULT 0,
            anomaly_score REAL NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, created_at DESC);"
    )
    conn.commit()


class DuplicateTransactionError(Exception):
    """Raised internally, never to the client - signals a replay."""


def create_transaction(conn: sqlite3.Connection, user_id: str, amount: float,
                        category: str, idempotency_key: str) -> dict:
    now = time.time()
    today = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()

    with _write_lock:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            existing = conn.execute(
                "SELECT * FROM transactions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                conn.execute("COMMIT;")
                return {
                    "transaction": dict(existing),
                    "is_replay": True,
                }

            # Last 5 transactions for this user, for burst/anomaly detection.
            recent_rows = conn.execute(
                """SELECT amount, created_at FROM transactions
                   WHERE user_id = ? ORDER BY created_at DESC LIMIT 5""",
                (user_id,),
            ).fetchall()
            recent = [(r["amount"], r["created_at"]) for r in recent_rows]

            stats = conn.execute(
                "SELECT * FROM user_stats WHERE user_id = ?", (user_id,)
            ).fetchone()

            old_anomaly = stats["anomaly_score"] if stats else 0.0
            burst_penalty = compute_burst_penalty(amount, now, recent)
            new_anomaly = update_anomaly_score(old_anomaly, burst_penalty)

            cur = conn.execute(
                """INSERT INTO transactions
                   (user_id, amount, category, idempotency_key, created_at, is_replay)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (user_id, amount, category, idempotency_key, now),
            )
            tx_id = cur.lastrowid

            if stats is None:
                conn.execute(
                    """INSERT INTO user_stats
                       (user_id, total_amount, transaction_count, first_seen_at,
                        last_seen_at, last_active_date, active_days, anomaly_score, version)
                       VALUES (?, ?, 1, ?, ?, ?, 1, ?, 1)""",
                    (user_id, amount, now, now, today, new_anomaly),
                )
            else:
                active_days = stats["active_days"]
                if stats["last_active_date"] != today:
                    active_days += 1
                conn.execute(
                    """UPDATE user_stats
                       SET total_amount = total_amount + ?,
                           transaction_count = transaction_count + 1,
                           last_seen_at = ?,
                           last_active_date = ?,
                           active_days = ?,
                           anomaly_score = ?,
                           version = version + 1
                       WHERE user_id = ?""",
                    (amount, now, today, active_days, new_anomaly, user_id),
                )

            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    tx_row = conn.execute(
        "SELECT * FROM transactions WHERE id = ?", (tx_id,)
    ).fetchone()
    return {"transaction": dict(tx_row), "is_replay": False}


def get_user_stats(conn: sqlite3.Connection, user_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM user_stats WHERE user_id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def get_all_user_stats(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM user_stats").fetchall()
    return [dict(r) for r in rows]
