"""单一入口。

用法：
    python -m src.main                # 启动监控守护（前台）
    python -m src.main seed           # 只构建一次名单并打印（不启动监控）
    python -m src.main status         # 查看名单/信号统计/限流状态
"""
import argparse
import logging
import sys

from src.config import get_config


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_seed() -> int:
    from src.smart.discovery import build_watchlist
    from src.store.db import Store
    cfg = get_config()
    store = Store(cfg.db_path)
    passed, rejected = build_watchlist(cfg.smart)
    store.upsert_wallets(passed)
    print(f"\n入围 {len(passed)} 个：")
    for w in passed:
        wr = f"{w.win_rate:.0%}" if w.win_rate is not None else "-"
        print(f"  {w.score:>5.1f} | 胜率{wr:>4} | PnL ${w.pnl:>10,.0f} | {w.name or w.address[:16]}…")
    print(f"\n淘汰 {len(rejected)} 个（前10）：")
    for w in rejected[:10]:
        print(f"  {w.address[:16]}… | {w.reason}")
    return 0


def cmd_status() -> int:
    from src.ratelimit import get_status
    from src.store.db import Store
    cfg = get_config()
    store = Store(cfg.db_path)
    wallets = store.active_wallets()
    print(f"名单：{len(wallets)} 个活跃钱包")
    import time
    with store._conn:
        n_signals = store._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        n_24h = store._conn.execute(
            "SELECT COUNT(*) FROM signals WHERE created_at > ?", (time.time() - 86400,)).fetchone()[0]
    print(f"信号：累计 {n_signals} 个 / 24h 内 {n_24h} 个")
    print("限流：", get_status())
    return 0


def cmd_run() -> int:
    from src.monitor.watcher import Watcher
    cfg = get_config()
    if not cfg.telegram.enabled:
        logging.getLogger(__name__).warning(
            "TG_BOT_TOKEN / TG_CHAT_ID 未配置，信号只写库不发通知")
    Watcher(cfg).run_forever()
    return 0


def cmd_lead() -> int:
    """Binance 领先信号监控（5 分钟/15 分钟 BTC 市场专用）。"""
    from src.monitor.binance_lead import run_lead_loop
    from src.notify.telegram import send_message
    cfg = get_config()
    if not cfg.telegram.enabled:
        logging.getLogger(__name__).warning(
            "TG_BOT_TOKEN / TG_CHAT_ID 未配置，领先信号只在终端显示")
    run_lead_loop(cfg.binance, telegram=send_message if cfg.telegram.enabled else None)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="freebuff", description="Polymarket 聪明钱跟踪 bot")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="启动监控守护（默认）")
    sub.add_parser("seed", help="只构建一次聪明钱名单")
    sub.add_parser("status", help="查看名单/信号/限流状态")
    sub.add_parser("lead", help="Binance 领先信号监控（5M BTC 市场专用）")
    args = parser.parse_args()

    cfg = get_config()
    _setup_logging(cfg.log_level)

    cmd = args.cmd or "run"
    if cmd == "run":
        return cmd_run()
    if cmd == "seed":
        return cmd_seed()
    if cmd == "status":
        return cmd_status()
    if cmd == "lead":
        return cmd_lead()
    parser.error(f"未知命令: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
