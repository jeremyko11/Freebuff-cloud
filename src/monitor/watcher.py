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

    def _notify_signal(self, s: Signal) -> None:
        # 终端彩色输出
        _COLORS = {"OPEN": "\033[92m", "ADD": "\033[93m", "REDUCE": "\033[91m", "SWEEP": "\033[95m"}
        _RESET = "\033[0m"
        _LABELS = {"OPEN": "🟢 新开仓", "ADD": "🟡 加仓", "REDUCE": "🔴 减仓/平仓", "SWEEP": "💸 拆单建仓"}
        c = _COLORS.get(s.type, "")
        r = _RESET
        who = s.wallet_name or f"{s.address[:8]}…{s.address[-4:]}"
        print(f"\n{c}{'='*50}")
        print(f"  {_LABELS.get(s.type, s.type)}  {who}")
        print(f"  市场：{s.title}")
        print(f"  方向：{s.side} {s.outcome} @ {s.price:.3f}")
        print(f"  金额：${s.usdc:,.0f}")
        if s.type == "SWEEP":
            print(f"  累积：{s.trade_count} 笔小单")
        if s.slug:
            print(f"  链接：https://polymarket.com/event/{s.slug}")
        print(f"{c}{'='*50}{r}")
        # 同时写日志
        logger.info("信号 [%s] %s %s %s $%.0f @%.3f", s.type, s.wallet_name or s.address[:10],
                    s.side, s.outcome, s.usdc, s.price)
        if self.cfg.telegram.enabled:
            send_message(self.cfg.telegram, format_signal(s))

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
    def run_forever(self) -> None:
        """主守护循环。"""
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        logger.info("Freebuff-cloud 启动：poll=%ds 名单=%d个",
                    self.cfg.monitor.poll_interval_sec, -1)
        self.refresh_watchlist()
        self.store.set_meta("last_seed_ts", str(time.time()))

        last_seed = time.time()
        last_verify = 0.0
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

            sleep_left = max(0, self.cfg.monitor.poll_interval_sec - (time.time() - t0))
            end = time.time() + sleep_left
            while not _stop and time.time() < end:
                time.sleep(min(2.0, end - time.time()))
        logger.info("已停止")
