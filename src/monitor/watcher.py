"""
监控守护：名单轮询 + 名单定期刷新 + 验证闭环回填。

主循环（每 poll_interval_sec 一轮）：
    for wallet in watchlist:
        acts = fetch_activity(wallet, start=游标)
        signals = detect_signals(...)
        notify + store
    每 refresh_hours 重建一次名单
    每小时回填一次待验证信号的 1h/24h 走势
"""
import logging
import signal
import time
from datetime import datetime, timezone

from src.api import data_api
from src.config import Config
from src.monitor.signals import Signal, detect_signals
from src.notify.telegram import send_message, format_signal
from src.smart.discovery import Wallet, build_watchlist
from src.store.db import Store

logger = logging.getLogger(__name__)

_stop = False


def _request_stop(signum, frame):
    global _stop
    _stop = True
    logger.info("收到信号 %s，准备退出", signum)


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Watcher:
    def __init__(self, cfg: Config, store: Store | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.watchlist: list[Wallet] = []

    # ------------------------------------------------------------------
    def refresh_watchlist(self) -> None:
        """重建聪明钱名单（入库 + 通知摘要）。"""
        t0 = time.time()
        passed, rejected = build_watchlist(self.cfg.smart)
        self.watchlist = passed
        self.store.upsert_wallets(passed)
        # 为每个钱包按历史信号推断主导市场类型
        for w in passed:
            try:
                self.store.compute_market_type(w.address)
            except Exception:
                pass
        dt = time.time() - t0
        logger.info("名单刷新完成：%d 个钱包，耗时 %.0fs", len(passed), dt)
        # 终端打印名单
        print(f"\n\033[96m{'='*50}")
        print(f"  📋 聪明钱名单（{len(passed)} 个入围 / {len(rejected)} 个淘汰）")
        print(f"  ⏱️  耗时 {dt:.0f}s")
        print(f"{'='*50}\033[0m")
        for i, w in enumerate(passed, 1):
            wr = f"{w.win_rate:.0%}" if w.win_rate is not None else "-"
            print(f"  {i:>2}. [{w.score:>5.1f}分] 胜率{wr:>4} | PnL ${w.pnl:>10,.0f} | {w.name or w.address[:16]}")
        print(f"\033[96m{'='*50}\033[0m\n")
        if self.cfg.telegram.enabled:
            lines = [f"<b>名单刷新</b> {_now_str()}（{dt:.0f}s）"]
            lines.append(f"入围 <b>{len(passed)}</b> 个 / 淘汰 {len(rejected)} 个")
            for w in passed[:10]:
                wr = f"{w.win_rate:.0%}" if w.win_rate is not None else "-"
                lines.append(f"  {w.score:>5.1f} | {wr:>4} | ${w.pnl:>10,.0f} | {w.name or w.address[:10]}…")
            if len(passed) > 10:
                lines.append(f"  … 共 {len(passed)} 个")
            send_message(self.cfg.telegram, "\n".join(lines))

    # ------------------------------------------------------------------
    def poll_once(self) -> int:
        """轮询全部钱包一轮，返回本轮信号数。"""
        n_signals = 0
        for w in self.watchlist:
            cursor = self.store.get_cursor(w.address)
            since = int(cursor / 1000) - 60 if cursor else int(time.time()) - 3600
            try:
                acts = data_api.fetch_activity(
                    w.address, limit=self.cfg.monitor.activity_limit, start_ts=since)
            except Exception as e:
                logger.warning("activity 拉取失败 %s: %s", w.address, e)
                continue
            if not acts:
                continue
            newest = max(a["timestamp"] for a in acts)
            self.store.set_cursor(w.address, newest)
            sigs = detect_signals(
                w.address, w.name, acts, self.store, self.cfg.monitor)
            for s in sigs:
                self.store.save_signal(s)
                n_signals += 1
                self._notify_signal(s)
        return n_signals

    @staticmethod
    def _perf_lines(pf: dict) -> list[str]:
        """钱包战绩 → 展示行。"""
        lines = []
        wr = f"{pf['win_rate']:.0%}" if pf.get("win_rate") is not None else "-"
        pnl = pf.get("pnl") or 0.0
        if pnl > 0:
            pnl_s = f"+${pnl:,.0f}"
        elif pnl < 0:
            pnl_s = f"-${-pnl:,.0f}"
        else:
            pnl_s = "$0"
        vol_s = f"${pf.get('volume') or 0:,.0f}"
        recent = f"{pf.get('recent_n') or 0}笔 · ${pf.get('recent_usdc') or 0:,.0f}"
        lines.append(f"📊 战绩：胜率{wr} · 盈亏{pnl_s} · 成交{vol_s}")
        today = f"{pf.get('today_n') or 0}笔 · 投注${pf.get('today_usdc') or 0:,.0f}"
        cum = f"{pf.get('cumulative_n') or 0}笔 · 投注${pf.get('cumulative_usdc') or 0:,.0f}"
        t_n = pf.get('today_n') or 0
        c_n = pf.get('cumulative_n') or 0
        if c_n == t_n:
            # 数据窗口内只有今日数据 -> 累计即今日，明确提示
            lines.append(f"今日：{today}（数据窗口内累计即今日）")
        else:
            lines.append(f"今日：{today} | 累计：{cum}")
        return lines

    def _notify_signal(self, s: Signal) -> None:
        # 注入钱包标签（自动+手动）供推送展示
        try:
            auto, manual, _ = self.store.wallet_tags(s.address)
            s.tags = manual + auto
        except Exception:
            s.tags = []
        perf = None
        try:
            perf = self.store.wallet_performance(s.address)
        except Exception:
            perf = None
        # 市场分类：缺失则探测一次（避免每次推送都算；算一次后库里有）
        market = self.store.get_market_type(s.address)
        if not market:
            try:
                market = self.store.compute_market_type(s.address)
            except Exception:
                market = ""
        # 来源标识：社区/手动推荐钱包（source 以 community: 开头）
        src_label = ""
        try:
            wrow = self.store.get_wallet(s.address)
            if wrow and wrow.get("source", "").startswith("community:"):
                src_label = wrow["source"].split(":", 1)[1]
                src_label = {"x": "X/Twitter", "reddit": "Reddit", "manual": "手动关注",
                             "custom": "自定义", "community": "社区推荐",
                             "smallcap": "小资金聪明钱"}.get(src_label, src_label or "社区推荐")
        except Exception:
            pass
        # 市场分类放信号开头第二行
        if market:
            from src.smart.market_tags import market_label
            s.market_label = market_label(market)
        # 终端彩色输出
        _COLORS = {"OPEN": "\033[92m", "ADD": "\033[93m", "REDUCE": "\033[91m", "SWEEP": "\033[95m"}
        _RESET = "\033[0m"
        _LABELS = {"OPEN": "🟢 新开仓", "ADD": "🟡 加仓", "REDUCE": "🔴 减仓/平仓", "SWEEP": "💸 拆单建仓"}
        c = _COLORS.get(s.type, "")
        r = _RESET
        who = s.wallet_name or f"{s.address[:8]}…{s.address[-4:]}"
        print(f"\n{c}{'='*50}")
        print(f"  {_LABELS.get(s.type, s.type)}  {who}")
        if s.market_label:
            print(f"  {s.market_label}")
        print(f"  市场：{s.title}")
        print(f"  方向：{s.side} {s.outcome} @ {s.price:.3f}")
        print(f"  金额：${s.usdc:,.0f}")
        if s.type == "SWEEP":
            print(f"  累积：{s.trade_count} 笔小单")
        if s.slug:
            print(f"  链接：https://polymarket.com/market/{s.slug}")
        print(f"{c}{'='*50}{r}")
        # 同时写日志（含标签）
        logger.info("信号 [%s] %s %s %s $%.0f @%.3f %s", s.type, s.wallet_name or s.address[:10],
                    s.side, s.outcome, s.usdc, s.price, " ".join(s.tags) if s.tags else "")
        if self.cfg.telegram.enabled:
            body = format_signal(s)
            if perf:
                body += "\n" + "\n".join(self._perf_lines(perf))
            if src_label:
                body += "\n🔗 来自 " + src_label + " 推荐"
            send_message(self.cfg.telegram, body)

    # ------------------------------------------------------------------
    def verify_pending(self) -> None:
        """验证闭环：对到期的信号回填 1h/24h 后价格，评估对错。"""
        if not self.cfg.monitor.verify_enabled:
            return
        pending = self.store.pending_verifications()
        if not pending:
            return
        # 简化：用市场当前 mid 近似（CLOB price 接口）；P0 只回填不评判
        from src.api.prices import fetch_mid
        for sig in pending:
            hours = (time.time() * 1000 - sig["ts"]) / 3600000
            if hours >= 24 and sig["verified_24h"] is None:
                mid = fetch_mid(sig["asset"])
                if mid is not None:
                    self.store.set_verification(sig["id"], "24h", mid)
            elif hours >= 1 and sig["verified_1h"] is None:
                mid = fetch_mid(sig["asset"])
                if mid is not None:
                    self.store.set_verification(sig["id"], "1h", mid)

    # ------------------------------------------------------------------
    def discover_sources(self) -> list[str]:
        """周期社交/全局发现：globaldiscover + hndiscover (+ xdiscover 需 X_BEARER)

        新钱包直接入库，社交脉搏信号单独推送；返回本次摘要行。
        """
        from src.smart import watchlist
        from src.smart.discovery import Wallet
        lines: list[str] = []
        existing = set(r["address"] for r in self.store._conn.execute(
            "SELECT address FROM wallets WHERE active=1"))

        # 1) 全局活跃盈利交易者（排行榜外）
        try:
            from src.smart.global_discover import discover_global_smart
            found = discover_global_smart(budget=10, existing=existing, sample_pages=4)
            added = 0
            for c in found:
                addr = c["address"]
                watchlist.add(self.cfg.smart.watchlist_path, addr, source="global",
                              note=f"全局活跃盈利 real+${c['realized_pnl']:.0f}")
                w = Wallet(address=addr, name=c["name"], source="community:global")
                w.pnl = c["realized_pnl"]; w.volume = c["volume"]
                self.store.upsert_wallets([w], reset_inactive=False)
                existing.add(addr)
                added += 1
            if added:
                names = ", ".join((c["name"] or c["address"][:10]) for c in found[:5])
                lines.append(f"🌍 全局发现 +{added}（{names}…）")
                logger.info("周期全局发现新增 %d 个", added)
        except Exception:
            logger.exception("周期全局发现异常")

        # 2) HackerNews 社交发现（免费）
        try:
            from src.smart.hn_discover import discover_hn_smart
            found = discover_hn_smart()
            added = 0
            for item in found:
                addr = item["address"]
                if addr in existing:
                    continue
                watchlist.add(self.cfg.smart.watchlist_path, addr, source="hn",
                              note=f"HN {item.get('source', '')}: {item.get('source_title', '')[:40]}")
                w = Wallet(address=addr, source="community:hn")
                w.extra = {"source_label": "HackerNews", "note": item.get("source", "")}
                self.store.upsert_wallets([w], reset_inactive=False)
                existing.add(addr)
                added += 1
            if added:
                lines.append(f"📰 HN 发现 +{added}")
                logger.info("周期 HN 发现新增 %d 个", added)
        except Exception:
            logger.exception("周期 HN 发现异常")

        # 3) X/Twitter 社交发现（需 X_BEARER，未配置则跳过）
        if self.cfg.x.bearer:
            try:
                from src.smart.xdiscover import x_mentions_listed
                found = x_mentions_listed(self.cfg.x)
                added = 0
                for item in found:
                    addr = item["address"].lower()
                    if addr in existing:
                        continue
                    watchlist.add(self.cfg.smart.watchlist_path, addr, source="x",
                                  note=f"X @{item['twitter_user']} 被推荐: {item['tweet_text'][:30]}")
                    w = Wallet(address=addr, source="community:x")
                    w.extra = {"source_label": "X/Twitter", "note": item["twitter_user"]}
                    self.store.upsert_wallets([w], reset_inactive=False)
                    existing.add(addr)
                    added += 1
                if added:
                    lines.append(f"🐦 X 发现 +{added}")
                    logger.info("周期 X 发现新增 %d 个", added)
            except Exception:
                logger.exception("周期 X 发现异常")

        # 4) 社交脉搏信号（LunarCrush，单独推送）
        try:
            from src.smart.social_pulse import (check_social_signals, format_social_signal,
                                                find_related_markets)
            sigs = check_social_signals()
            if sigs:
                lines.append(f"📡 社交信号 x{len(sigs)}")
                for sig in sigs:
                    text = format_social_signal(sig)
                    kw = sig.get("keyword", "")
                    if kw:
                        related = find_related_markets(kw, n=2)
                        if related:
                            text += "\n\n🎯 相关市场："
                            for m in related:
                                q = m["question"]
                                if len(q) > 60:
                                    q = q[:60] + "…"
                                text += (f"\n<a href='https://polymarket.com/market/{m['slug']}'>"
                                         f"{q}</a> (${m['volume']:,.0f} vol)")
                    if self.cfg.telegram.enabled:
                        send_message(self.cfg.telegram, text)
                    else:
                        logger.info("社交信号: %s", text.replace("\n", " | "))
        except Exception:
            logger.exception("周期社交脉搏异常")

        if lines:
            logger.info("周期发现完成：%s", "；".join(lines))
            if self.cfg.telegram.enabled:
                send_message(self.cfg.telegram, "\n".join(["🧭 <b>发现源巡检</b>"] + lines))
        return lines

    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        """主守护循环。"""
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        logger.info("Freebuff-cloud 启动：poll=%ds 名单=%d个",
                    self.cfg.monitor.poll_interval_sec, -1)
        self.refresh_watchlist()
        self.store.set_meta("last_seed_ts", str(time.time()))
        # 启动时先跑一轮发现源（随后按 discover_hours 周期巡检）
        if self.cfg.smart.discover_hours > 0:
            try:
                self.discover_sources()
            except Exception:
                logger.exception("启动发现异常")

        last_seed = time.time()
        last_verify = 0.0
        last_discover = 0.0
        global _stop
        while not _stop:
            t0 = time.time()
            try:
                n = self.poll_once()
                logger.info("轮询完成：%d 个钱包，%d 个信号，耗时 %.0fs",
                            len(self.watchlist), n, time.time() - t0)
            except Exception:
                logger.exception("轮询异常，继续下一轮")

            if time.time() - last_seed > self.cfg.smart.refresh_hours * 3600:
                self.refresh_watchlist()
                self.store.set_meta("last_seed_ts", str(time.time()))
                last_seed = time.time()

            if time.time() - last_verify > 3600:
                try:
                    self.verify_pending()
                except Exception:
                    logger.exception("验证回填异常")
                last_verify = time.time()

            if (self.cfg.smart.discover_hours > 0
                    and time.time() - last_discover > self.cfg.smart.discover_hours * 3600):
                try:
                    self.discover_sources()
                except Exception:
                    logger.exception("周期发现异常")
                last_discover = time.time()

            sleep_left = max(0, self.cfg.monitor.poll_interval_sec - (time.time() - t0))
            end = time.time() + sleep_left
            while not _stop and time.time() < end:
                time.sleep(min(2.0, end - time.time()))
        logger.info("已停止")
