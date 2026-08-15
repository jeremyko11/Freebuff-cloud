"""SQLite 存储：钱包名单、信号、轮询游标、验证回填、元数据。"""
import logging
import sqlite3
import threading
import time
from datetime import datetime
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
    manual_tags TEXT DEFAULT '',
    market_type TEXT DEFAULT ''
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
    verified_24h REAL,
    market_category TEXT,
    market_league TEXT,
    wallet_source_type TEXT
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
CREATE TABLE IF NOT EXISTS market_categories (
    prefix TEXT PRIMARY KEY,
    level TEXT,
    category TEXT,
    league TEXT,
    emoji TEXT,
    ord INTEGER
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
        self._ensure_classification()

    def _ensure_classification(self) -> None:
        """迁移旧库：给 signals 加分类列、填充 market_categories 字典、回填存量分类。"""
        # 1) add missing columns on legacy db
        with self._lock, self._conn:
            sig_cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(signals)")]
            for col in ("market_category", "market_league", "wallet_source_type"):
                if col not in sig_cols:
                    self._conn.execute(f"ALTER TABLE signals ADD COLUMN {col} TEXT")
            # 2) seed category dict
            from src.smart.market_tags import market_dict
            self._conn.execute("DELETE FROM market_categories")
            for row in market_dict():
                self._conn.execute(
                    "INSERT OR REPLACE INTO market_categories (prefix,level,category,league,emoji,ord) "
                    "VALUES (?,?,?,?,?,?)",
                    (row["prefix"], row["level"], row["category"], row["league"], row["emoji"], row["ord"]))
            # 3) backfill existing signals (only where missing)
            from src.smart.market_tags import classify_slug
            rows = self._conn.execute(
                "SELECT id, slug, address FROM signals WHERE (market_category IS NULL OR market_category='')"
            ).fetchall()
            if rows:
                blank = self._conn.execute(
                    "SELECT address, source FROM wallets").fetchall()
                src_map = {r["address"]: (r["source"] or "") for r in blank}
                n = 0
                for r in rows:
                    cat, league = classify_slug(r["slug"] or "")
                    source_raw = src_map.get(r["address"], "")
                    stype = "排行榜"
                    if source_raw.startswith("community:smallcap"):
                        stype = "小资金发现"
                    elif source_raw.startswith("community:"):
                        stype = "社区推荐"
                    elif source_raw == "manual":
                        stype = "手动关注"
                    elif source_raw.startswith("lb") or source_raw == "":
                        stype = "排行榜"
                    self._conn.execute(
                        "UPDATE signals SET market_category=?, market_league=?, wallet_source_type=? WHERE id=?",
                        (cat, league, stype, r["id"]))
                    n += 1
                logger.info("分类回填 %d 条存量信号", n)
            # 3b) 分析视图（回溯/回测/分析用）
            self._conn.executescript("""
                DROP VIEW IF EXISTS vw_signals_classified;
                CREATE VIEW vw_signals_classified AS
                  SELECT id, ts, created_at, address, wallet_name, type, side,
                         outcome, title, slug, usdc, price, trade_count,
                         notified, price_at_signal, verified_1h, verified_24h,
                         market_category, market_league, wallet_source_type,
                         COALESCE(market_category,'') || '-' || COALESCE(market_league,'') AS market_key
                  FROM signals;

                DROP VIEW IF EXISTS vw_signals_by_category;
                CREATE VIEW vw_signals_by_category AS
                  SELECT market_category,
                         COUNT(*) AS n_signals,
                         SUM(usdc) AS total_usdc,
                         ROUND(AVG(price),3) AS avg_price,
                         COUNT(DISTINCT address) AS n_wallets,
                         SUM(CASE WHEN type='OPEN' THEN 1 ELSE 0 END) AS n_open,
                         SUM(CASE WHEN type='ADD' THEN 1 ELSE 0 END) AS n_add,
                         SUM(CASE WHEN type='REDUCE' THEN 1 ELSE 0 END) AS n_reduce,
                         SUM(CASE WHEN type='SWEEP' THEN 1 ELSE 0 END) AS n_sweep
                  FROM signals WHERE market_category IS NOT NULL
                  GROUP BY market_category ORDER BY n_signals DESC;

                DROP VIEW IF EXISTS vw_signals_by_wallet;
                CREATE VIEW vw_signals_by_wallet AS
                  SELECT address, wallet_name, wallet_source_type,
                         COUNT(*) AS n_signals,
                         SUM(usdc) AS total_usdc,
                         COUNT(DISTINCT market_category) AS n_categories,
                         MAX(market_category) AS top_category,
                         COUNT(DISTINCT market_league) AS n_leagues
                  FROM signals GROUP BY address, wallet_name, wallet_source_type
                  ORDER BY n_signals DESC;
            """)

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
                src = getattr(w, "source", "")
                is_store_sourced = (src == "manual" or str(src).startswith("community:"))
                if is_store_sourced:
                    # 社区/手动/小资金钱包：不覆盖 pb为排行榜口径的 pnl/volume/score，避免冲掉本地统计
                    self._conn.execute(
                        'INSERT INTO wallets (address,name,pnl,volume,win_rate,profit_factor,'
                        'closed_count,score,source,active,updated_at,auto_tags) '
                        'VALUES (?,?,?,?,?,?,?,?,?,1,?,?) '
                        'ON CONFLICT(address) DO UPDATE SET '
                        'active=1, updated_at=excluded.updated_at, auto_tags=excluded.auto_tags',
                        (w.address, w.name, w.pnl, w.volume, w.win_rate, w.profit_factor,
                         w.closed_count, w.score, w.source, now, auto))
                else:
                    self._conn.execute(
                        'INSERT INTO wallets (address,name,pnl,volume,win_rate,profit_factor,'
                        'closed_count,score,source,active,updated_at,auto_tags) '
                        'VALUES (?,?,?,?,?,?,?,?,?,1,?,?) '
                        'ON CONFLICT(address) DO UPDATE SET '
                        'name=excluded.name, pnl=excluded.pnl, volume=excluded.volume, '
                        'win_rate=excluded.win_rate, profit_factor=excluded.profit_factor, '
                        'closed_count=excluded.closed_count, score=excluded.score, '
                        'source=excluded.source, active=1, updated_at=excluded.updated_at, '
                        'auto_tags=excluded.auto_tags',
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


    def wallet_performance(self, address: str, days: int = 7) -> dict | None:
        """钱包战绩：累计（pnl/胜率/成交额）+ 近期活跃（近 days 天信号/投入）。

        返回 dict 或 None（钱包不在名单）。
        """
        with self._lock:
            w = self._conn.execute(
                "SELECT pnl,volume,win_rate,profit_factor,closed_count,auto_tags FROM wallets WHERE address=?",
                (address,)).fetchone()
            if not w:
                return None
            since = time.time() - days * 86400
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(usdc),0) AS usdc "
                "FROM signals WHERE address=? AND created_at>?",
                (address, since)).fetchone()
            # 当日（本地时区从 0 点起）
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            trow = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(usdc),0) AS usdc "
                "FROM signals WHERE address=? AND created_at>=?",
                (address, today_start)).fetchone()
            # 全周期累计（从库最早记录）
            crow = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(usdc),0) AS usdc "
                "FROM signals WHERE address=?", (address,)).fetchone()
        return {
            "pnl": w["pnl"] or 0.0,
            "volume": w["volume"] or 0.0,
            "win_rate": w["win_rate"],
            "profit_factor": w["profit_factor"],
            "closed_count": w["closed_count"] or 0,
            "recent_n": row["n"],
            "recent_usdc": row["usdc"],
            "days": days,
            "today_n": trow["n"],
            "today_usdc": trow["usdc"],
            "cumulative_n": crow["n"],
            "cumulative_usdc": crow["usdc"],
        }


    def backfill_asset(self, condition_id: str) -> dict:
        """给一个 condition_id 的所有实际查询 token id（gamma clobTokenIds）并回填 signals.asset。"""
        import urllib.parse
        from src.api.data_api import _get_session
        if not condition_id:
            return {}
        cid = condition_id.strip().lower()
        try:
            url = "https://gamma-api.polymarket.com/markets?" + urllib.parse.urlencode({"condition_ids": cid})
            resp = _get_session().get(url, timeout=8)
            if resp.status_code == 429:
                return {}
            resp.raise_for_status()
            markets = resp.json()
            if not markets:
                return {}
            m = markets[0]
            import json as _json
            outcomes = m.get("outcomes") or []
            token_ids = m.get("clobTokenIds") or []
            # gamma 返回的是 JSON 字符串，需解析为数组
            if isinstance(outcomes, str):
                try:
                    outcomes = _json.loads(outcomes)
                except Exception:
                    outcomes = []
            if isinstance(token_ids, str):
                try:
                    token_ids = _json.loads(token_ids)
                except Exception:
                    token_ids = []
            if not outcomes or not token_ids or len(outcomes) != len(token_ids):
                return {}
            mapping = {str(o).strip().lower(): t for o, t in zip(outcomes, token_ids)}
            return mapping
        except Exception as e:
            logger.debug("gamma backfill 失败 %s: %s", cid[:16], e)
            return {}

    def wallet_today_pnl(self, address: str) -> dict:
        """估算钱包今日盈亏（用当前市场价 vs 买入价，仅对有 asset 的信号）。

        返回 {n_estimated, total_pnl, win_count, loss_count}。
        估算方法：买入信号 BUY => pnl ≈ usdc*(cur/buy - 1)；SELL 反向。
        cur 取 market 当前 mid（fetch_mid），无 asset 或取价失败则跳过该条。
        """
        from datetime import datetime
        from src.api.prices import fetch_mid
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, side, usdc, price, asset FROM signals "
                "WHERE address=? AND created_at>=? AND asset!=''",
                (address, today_start)).fetchall()
        total = 0.0
        wins = 0
        losses = 0
        n_est = 0
        for r in rows:
            cur = fetch_mid(r["asset"])
            if cur is None or (r["price"] or 0) <= 0:
                continue
            ratio = cur / r["price"] - 1.0
            if r["side"] == "SELL":
                ratio = -ratio
            pnl = (r["usdc"] or 0) * ratio
            total += pnl
            n_est += 1
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        return {"n_estimated": n_est, "total_pnl": total, "win_count": wins, "loss_count": losses}

    def compute_market_type(self, address: str) -> str:
        """根据该钱包历史信号的 slug 推断主导市场类型并写回。"""
        from src.smart.market_tags import wallet_market_type
        with self._lock:
            rows = self._conn.execute(
                "SELECT slug FROM signals WHERE address=? AND slug != ''", (address,)).fetchall()
        slugs = [r["slug"] for r in rows]
        mtype = wallet_market_type(slugs) or ""
        if mtype:
            with self._lock:
                self._conn.execute(
                    "UPDATE wallets SET market_type=? WHERE address=?", (mtype, address))
        return mtype

    def get_market_type(self, address: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT market_type FROM wallets WHERE address=?", (address,)).fetchone()
        return (row["market_type"] if row else "") or ""

    # ---------------- signals ----------------

    def signal_seen(self, dedup_key: str, window_sec: int) -> bool:
        since = time.time() - window_sec
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM signals WHERE dedup_key=? AND created_at>? LIMIT 1",
                (dedup_key, since)).fetchone()
        return row is not None

    def save_signal(self, s, asset: str = "") -> None:
        # 实时分类：市场分类（slug）+ 来源类型（钱包 source）
        from src.smart.market_tags import classify_slug
        cat, league = classify_slug(s.slug or "")
        s_asset = getattr(s, "asset", "") or ""
        stype = "排行榜"
        try:
            src = self._conn.execute(
                "SELECT source FROM wallets WHERE address=?", (s.address,)).fetchone()
            raw = (src["source"] if src else "") or ""
            if raw.startswith("community:smallcap"):
                stype = "小资金发现"
            elif raw.startswith("community:"):
                stype = "社区推荐"
            elif raw == "manual":
                stype = "手动关注"
            elif raw.startswith("lb") or raw == "":
                stype = "排行榜"
        except Exception:
            stype = "排行榜"
        s._category = cat
        s._league = league
        s._source_type = stype
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO signals (created_at,ts,address,wallet_name,type,side,
                     condition_id,asset,outcome,title,slug,usdc,price,trade_count,
                     tx_hashes,dedup_key,notified,price_at_signal,
                     market_category,market_league,wallet_source_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                (time.time(), s.ts, s.address, s.wallet_name, s.type, s.side,
                 s.conditionId, s_asset or asset or "", s.outcome, s.title, s.slug, s.usdc,
                 s.price, s.trade_count, ",".join(s.tx_hashes), s.dedup_key, s.price,
                 cat, league, stype))
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
