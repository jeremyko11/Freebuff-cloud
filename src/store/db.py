"""SQLite 存储：钱包名单、信号、轮询游标、验证回填、元数据。"""
import logging
import sqlite3
import threading
import time
from pathlib import Path

from src.smart.tagging import derive_auto_tags

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    name TEXT,
    pnl REAL,
    volume REAL,
    win_rate REAL,
    profit_factor REAL,
    closed_count INTEGER,
    score REAL,
    source TEXT,
    active INTEGER DEFAULT 1,
    updated_at REAL,
    auto_tags TEXT DEFAULT '',
    manual_tags TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL,
    ts REAL,
    address TEXT,
    wallet_name TEXT,
    type TEXT,
    side TEXT,
    condition_id TEXT,
    asset TEXT,
    outcome TEXT,
    title TEXT,
    slug TEXT,
    usdc REAL,
    price REAL,
    trade_count INTEGER,
    tx_hashes TEXT,
    dedup_key TEXT,
    notified INTEGER DEFAULT 0,
    price_at_signal REAL,
    verified_1h REAL,
    verified_24h REAL
);
CREATE INDEX IF NOT EXISTS idx_signals_dedup ON signals(dedup_key, created_at);
CREATE INDEX IF NOT EXISTS idx_signals_addr ON signals(address, ts DESC);
CREATE TABLE IF NOT EXISTS cursors (
    address TEXT PRIMARY KEY,
    last_ts REAL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS wallet_markets (
    address TEXT,
    condition_id TEXT,
    first_seen REAL,
    PRIMARY KEY (address, condition_id)
);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------------- wallets ----------------

    def upsert_wallets(self, wallets: list) -> None:
        now = time.time()
        with self._lock, self._conn:
            for w in wallets:
                auto = ",".join(derive_auto_tags(
                    getattr(w, "pnl", None), getattr(w, "volume", None),
                    getattr(w, "win_rate", None), getattr(w, "profit_factor", None),
                    getattr(w, "closed_count", None), getattr(w, "score", None)))
                self._conn.execute(
                    """INSERT INTO wallets (address,name,pnl,volume,win_rate,profit_factor,
                         closed_count,score,source,active,updated_at,auto_tags)
                       VALUES (?,?,?,?,?,?,?,?,?,1,?,?)
                       ON CONFLICT(address) DO UPDATE SET
                         name=excluded.name, pnl=excluded.pnl, volume=excluded.volume,
                         win_rate=excluded.win_rate, profit_factor=excluded.profit_factor,
                         closed_count=excluded.closed_count, score=excluded.score,
                         source=excluded.source, active=1, updated_at=excluded.updated_at,
                         auto_tags=excluded.auto_tags""",
                    (w.address, w.name, w.pnl, w.volume, w.win_rate, w.profit_factor,
                     w.closed_count, w.score, w.source, now, auto))
            # 不在新名单里的标 inactive
            if wallets:
                addrs = [w.address for w in wallets]
                placeholders = ",".join("?" * len(addrs))
                self._conn.execute(
                    f"UPDATE wallets SET active=0 WHERE address NOT IN ({placeholders})", addrs)

    def active_wallets(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM wallets WHERE active=1 ORDER BY score DESC").fetchall()
        return [dict(r) for r in rows]

    # ---------------- tags ----------------

    def add_manual_tag(self, address: str, tag: str) -> int:
        """给钱包追加一个手动标签，返回该钱包当前手动标签数。"""
        tag = tag.strip()
        if not tag:
            return 0
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT manual_tags FROM wallets WHERE address=?", (address,)).fetchone()
            if not row:
                return 0
            cur = [t for t in (row["manual_tags"] or "").split(",") if t]
            if tag not in cur:
                cur.append(tag)
            self._conn.execute(
                "UPDATE wallets SET manual_tags=? WHERE address=?", (",".join(cur), address))
            return len(cur)

    def remove_manual_tag(self, address: str, tag: str) -> int:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT manual_tags FROM wallets WHERE address=?", (address,)).fetchone()
            if not row:
                return 0
            cur = [t for t in (row["manual_tags"] or "").split(",") if t and t != tag]
            self._conn.execute(
                "UPDATE wallets SET manual_tags=? WHERE address=?", (",".join(cur), address))
            return len(cur)

    def clear_manual_tags(self, address: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE wallets SET manual_tags='' WHERE address=?", (address,))

    def get_wallet(self, address: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wallets WHERE address=?", (address,)).fetchone()
        return dict(row) if row else None

    def wallet_tags(self, address_or_name: str) -> tuple[list[str], list[str], str | None]:
        """按地址精确 / 名称模糊查询，返回 (auto_tags, manual_tags, address)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT address,auto_tags,manual_tags FROM wallets WHERE address=?",
                (address_or_name,)).fetchone()
            if not row:
                row = self._conn.execute(
                    "SELECT address,auto_tags,manual_tags FROM wallets WHERE name=?",
                    (address_or_name,)).fetchone()
            if not row:
                return [], [], None
            auto = [t for t in (row["auto_tags"] or "").split(",") if t]
            manual = [t for t in (row["manual_tags"] or "").split(",") if t]
            return auto, manual, row["address"]

    # ---------------- signals ----------------

    def signal_seen(self, dedup_key: str, window_sec: int) -> bool:
        since = time.time() - window_sec
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM signals WHERE dedup_key=? AND created_at>? LIMIT 1",
                (dedup_key, since)).fetchone()
        return row is not None

    def save_signal(self, s, asset: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO signals (created_at,ts,address,wallet_name,type,side,
                     condition_id,asset,outcome,title,slug,usdc,price,trade_count,
                     tx_hashes,dedup_key,notified,price_at_signal)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (time.time(), s.ts, s.address, s.wallet_name, s.type, s.side,
                 s.conditionId, asset or "", s.outcome, s.title, s.slug, s.usdc,
                 s.price, s.trade_count, ",".join(s.tx_hashes), s.dedup_key, s.price))
            if s.type == "OPEN":
                self._conn.execute(
                    """INSERT OR IGNORE INTO wallet_markets (address, condition_id, first_seen)
                       VALUES (?,?,?)""",
                    (s.address, s.conditionId, time.time()))

    def get_wallet_markets(self, address: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT condition_id FROM wallet_markets WHERE address=?", (address,)).fetchall()
        return {r[0] for r in rows}

    def pending_verifications(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, ts, asset, side, price, verified_1h, verified_24h
                   FROM signals WHERE asset != ''
                     AND (verified_1h IS NULL OR verified_24h IS NULL)
                   ORDER BY ts ASC LIMIT 100""").fetchall()
        return [dict(r) for r in rows]

    def set_verification(self, sig_id: int, kind: str, mid: float) -> None:
        col = "verified_1h" if kind == "1h" else "verified_24h"
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE signals SET {col}=? WHERE id=?", (mid, sig_id))

    # ---------------- cursors / meta ----------------

    def get_cursor(self, address: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_ts FROM cursors WHERE address=?", (address,)).fetchone()
        return row[0] if row else 0.0

    def set_cursor(self, address: str, ts_ms: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO cursors (address, last_ts) VALUES (?,?)
                   ON CONFLICT(address) DO UPDATE SET last_ts=excluded.last_ts""",
                (address, ts_ms))

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO meta (key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value))
