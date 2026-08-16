"""RTDS 实时监控：订阅全市场成交流，过滤目标钱包，亚秒级推送。

替代 2s 轮询检测。目标钱包来自 wallets(active=1)。
每笔成交（RTDS activity/trades, 免鉴权）直接转 Signal：
  - OPEN/ADD 由 wallet_markets 判断（钱包是否已有该市场持仓）
  - 去重用 dedup_key（同钱包/市场/方向/小时）
  - usdc = size * price
"""
import logging
import threading
import time

from src.api.rtds import RtdsClient
from src.config import Config
from src.monitor.signals import Signal
from src.notify.telegram import send_message, format_signal
from src.store.db import Store

logger = logging.getLogger(__name__)

_stop = threading.Event()


def _hour_bucket(ts_sec: float) -> int:
    return int(ts_sec // 3600)


class RtdsWatcher:
    def __init__(self, cfg: Config, store: Store | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.targets: set[str] = set()
        self.wallet_cache: dict[str, str] = {}  # addr -> source

    def _refresh_targets(self) -> None:
        """从 wallets(active=1) 刷新目标钱包地址集合。"""
        try:
            rows = self.store._conn.execute(
                "SELECT address, source FROM wallets WHERE active=1").fetchall()
            addrs = set()
            for r in rows:
                a = (r["address"] or "").lower()
                if a:
                    addrs.add(a)
                    self.wallet_cache[a] = (r["source"] or "")
            self.targets = addrs
            logger.info("RTDS 目标钱包刷新: %d 个", len(addrs))
        except Exception as e:
            logger.warning("刷新目标钱包失败: %s", e)

    def _build_signal(self, t: dict) -> Signal | None:
        """RTDS payload -> Signal（OPEN/ADD/REDUCE）。SWEEP 拆单暂不做。"""
        addr = (t.get("proxyWallet") or "").lower()
        side = (t.get("side") or "").upper()
        if not addr or side not in ("BUY", "SELL"):
            return None
        ts_sec = t.get("timestamp") or 0
        price = float(t.get("price") or 0)
        size = float(t.get("size") or 0)
        usdc = size * price
        # 金额门槛（沿用轮询同款 MON_MIN_SIGNAL_USDC）
        if usdc < self.cfg.monitor.min_signal_usdc:
            return None
        cid = t.get("conditionId") or ""
        outcome = t.get("outcome") or ""
        title = t.get("title") or ""
        slug = t.get("slug") or ""
        # 缺市场字段时用 asset 反查补全（APP/REDEEM 等成交常不带）
        if (not cid or not slug) and t.get("asset"):
            try:
                from src.api.rtds import lookup_market_by_asset
                info = lookup_market_by_asset(str(t["asset"]))
                if info:
                    cid = cid or info.get("conditionId") or ""
                    slug = slug or info.get("slug") or ""
                    title = title or info.get("title") or ""
                    outcome = outcome or info.get("outcome") or ""
            except Exception:
                pass
        # OPEN/ADD/REDUCE
        if side == "SELL":
            stype = "REDUCE"
        else:
            known = self.store.get_wallet_markets(addr)
            stype = "OPEN" if cid not in known else "ADD"
            if stype == "OPEN":
                self.store._conn.execute(
                    "INSERT OR IGNORE INTO wallet_markets (address, condition_id, first_seen) VALUES (?,?,?)",
                    (addr, cid, ts_sec or time.time()))
                self.store._conn.commit()
        key = f"{addr}:{cid}:{outcome}:{side}:{_hour_bucket(ts_sec)}"
        if self.store.signal_seen(key, self.cfg.monitor.dedup_window_sec):
            return None
        name = t.get("name") or t.get("pseudonym") or addr[:10]
        return Signal(
            address=addr,
            wallet_name=name,
            type=stype,
            side=side,
            conditionId=cid,
            outcome=outcome,
            title=title,
            slug=slug,
            asset=t.get("asset") or "",
            usdc=usdc,
            price=price,
            tx_hashes=[t.get("transactionHash")] if t.get("transactionHash") else [],
            ts=(ts_sec * 1000) if ts_sec < 1e12 else ts_sec,  # → 毫秒
            dedup_key=key,
        )

    def _on_trade(self, addr: str, t: dict) -> None:
        """RTDS 每笔成交回调：仅处理目标钱包。"""
        if addr not in self.targets:
            return
        try:
            s = self._build_signal(t)
            if not s:
                return
            self.store.save_signal(s)
            self._notify(s)
        except Exception as e:
            logger.warning("RTDS 信号处理失败 %s: %s", addr[:12], e)

    def _notify(self, s: Signal) -> None:
        # 计算把握度：低把握不推送（减少噪音），高把握标注
        conf = 100.0
        try:
            wr = None
            row = self.store._conn.execute(
                "SELECT win_rate FROM wallets WHERE address=?", (s.address,)).fetchone()
            if row:
                wr = row["win_rate"]
            from src.smart.confidence import confidence, should_push, format_conf
            conf = confidence(wr, s.price, s.usdc)
            if not should_push(conf):
                logger.info("低把握信号[跳过推送] %s %s %.0f分", s.wallet_name or s.address[:10], s.type, conf)
                return
        except Exception:
            pass
        # 多维过滤（市场/钱包/金额/来源）
        try:
            from src.smart.filter import should_push
            ok, reason = should_push(s, self.store)
            if not ok:
                logger.info("过滤[跳过] %s %s: %s", s.wallet_name or s.address[:10], s.type, reason)
                return
        except Exception:
            pass
        # 注入标签/市场分类（与轮询一致）
        try:
            auto, manual, _ = self.store.wallet_tags(s.address)
            s.tags = manual + auto
        except Exception:
            s.tags = []
        try:
            from src.smart.market_tags import market_label, classify_slug
            market = self.store.get_market_type(s.address)
            if not market:
                # wallets.market_type 空时用信号 slug 现场分类兜底
                market, _ = classify_slug(s.slug or "")
            if market:
                s.market_label = market_label(market)
        except Exception:
            pass
        logger.info("RTDS 信号 [%s] %s %s %s $%.0f @%.3f %s", s.type,
                    s.wallet_name or s.address[:10], s.side, s.outcome, s.usdc,
                    s.price or 0, " ".join(s.tags) if s.tags else "")
        if self.cfg.telegram.enabled:
            body = format_signal(s)
            from src.smart.confidence import format_conf
            body += "\n" + format_conf(conf)
            send_message(self.cfg.telegram, body)

    def run_forever(self) -> None:
        self._refresh_targets()
        logger.info("RTDS 监控启动：目标 %d 钱包", len(self.targets))
        client = RtdsClient(on_trade=self._on_trade, on_status=lambda m: logger.info("RTDS %s", m))
        client.start()
        last_refresh = time.time()
        while not _stop.is_set():
            # 定期刷新目标钱包（名单可能更新）
            if time.time() - last_refresh > 1800:
                self._refresh_targets()
                last_refresh = time.time()
            time.sleep(5)
        client.stop()
        logger.info("RTDS 监控已停止")


def _handle_sig(signum, frame):
    _stop.set()
