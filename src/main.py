"""单一入口。

用法：
    python -m src.main                      # 启动监控守护（前台）
    python -m src.main seed                 # 只构建一次名单并打印（不启动监控）
    python -m src.main status               # 查看名单/信号/限流状态
    python -m src.main tag list <钱包名|地址> # 查看某钱包标签（默认：显示全部带标签的钱包）
    python -m src.main tag add <钱包名|地址> <标签...>    # 追加手动标签
    python -m src.main tag rm  <钱包名|地址> <标签...>    # 移除手动标签
    python -m src.main tag clear <钱包名|地址>           # 清空手动标签
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


def _fmt_tags(auto: list[str], manual: list[str]) -> str:
    from src.smart.tagging import with_emoji
    parts = [f"[{with_emoji(t)}]" for t in manual]
    parts += [f"<{with_emoji(t)}>" for t in auto]
    return " ".join(parts) if parts else "-"


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
        auto, manual, _ = store.wallet_tags(w.address)
        print(f"  {w.score:>5.1f} | 胜率{wr:>4} | PnL ${w.pnl:>10,.0f} | {w.name or w.address[:16]}… {_fmt_tags(auto, manual)}")
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
    for w in wallets:
        auto = [t for t in (w.get("auto_tags") or "").split(",") if t]
        manual = [t for t in (w.get("manual_tags") or "").split(",") if t]
        wr = f"{w['win_rate']:.0%}" if w["win_rate"] is not None else "-"
        name = w["name"] or w["address"][:16]
        print(f"  {w['score']:>5.1f} | {wr:>4} | ${w['pnl'] or 0:>10,.0f} | {name:<24} {_fmt_tags(auto, manual)}")
    import time
    with store._conn:
        n_signals = store._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        n_24h = store._conn.execute(
            "SELECT COUNT(*) FROM signals WHERE created_at > ?", (time.time() - 86400,)).fetchone()[0]
    print(f"信号：累计 {n_signals} 个 / 24h 内 {n_24h} 个")
    print("限流：", get_status())
    return 0


def cmd_tag(args) -> int:
    from src.store.db import Store
    cfg = get_config()
    store = Store(cfg.db_path)

    op = args.tag_op
    if op == "list":
        if args.who:
            auto, manual, addr = store.wallet_tags(args.who)
            if addr is None:
                print(f"未找到钱包：{args.who}")
                return 1
            row = store.get_wallet(addr)
            name = row["name"] or addr[:16]
            print(f"{name}  {addr}")
            print(f" 自动标签: {' '.join('['+t+']' for t in auto) if auto else '-'}")
            print(f" 手动标签: {' '.join('['+t+']' for t in manual) if manual else '-'}")
        else:
            print("列出所有带手动标签的钱包：")
            n = 0
            for w in store.active_wallets():
                manual = [t for t in (w.get("manual_tags") or "").split(",") if t]
                if manual:
                    n += 1
                    name = w["name"] or w["address"][:16]
                    print(f"  {name:<24} {' '.join('['+t+']' for t in manual)}")
            if n == 0:
                print("  （尚无手动标签）")
        return 0

    if not args.who or (not args.tag_values and args.tag_op != "clear"):
        print("用法：python -m src.main tag <add|rm|clear|list> <钱包名|地址> [标签...]")
        return 2

    auto, manual, addr = store.wallet_tags(args.who)
    if addr is None:
        print(f"未找到钱包：{args.who}")
        return 1
    row = store.get_wallet(addr)
    name = row["name"] or addr[:16]

    if op == "add":
        for t in args.tag_values:
            store.add_manual_tag(addr, t)
        _, manual_after, _ = store.wallet_tags(addr)
        print(f"已为 {name} 追加手动标签：{' '.join('['+t+']' for t in manual_after)}")
    elif op == "rm":
        for t in args.tag_values:
            store.remove_manual_tag(addr, t)
        _, manual_after, _ = store.wallet_tags(addr)
        print(f"移除后 {name} 手动标签：{' '.join('['+t+']' for t in manual_after) if manual_after else '(空)'}")
    elif op == "clear":
        store.clear_manual_tags(addr)
        print(f"已清空 {name} 所有手动标签")
    else:
        print(f"未知操作：{op}")
        return 2
    return 0


def cmd_run() -> int:
    from src.monitor.watcher import Watcher
    cfg = get_config()
    if not cfg.telegram.enabled:
        logging.getLogger(__name__).warning(
            "TG_BOT_TOKEN / TG_CHAT_ID 未配置，信号只写库不发通知")
    Watcher(cfg).run_forever()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="freebuff", description="Polymarket 聪明钱跟踪 bot")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="启动监控守护（默认）")
    sub.add_parser("seed", help="只构建一次聪明钱名单")
    sub.add_parser("status", help="查看名单/信号/限流状态")
    tag_p = sub.add_parser("tag", help="管理钱包标签")
    tag_sub = tag_p.add_subparsers(dest="tag_op")
    tag_list = tag_sub.add_parser("list", help="查看标签")
    tag_list.add_argument("who", nargs="?", default=None, help="钱包名或地址（可选）")
    for op in ("add", "rm", "clear"):
        p = tag_sub.add_parser(op, help=f"{op} 标签")
        p.add_argument("who", help="钱包名或地址")
        p.add_argument("tag_values", nargs="*", help="标签（可多个）")
    args = parser.parse_args()

    cfg = get_config()
    _setup_logging(cfg.log_level)

    cmd = args.cmd or "run"
    if cmd == "tag":
        if args.tag_op is None:
            tag_p.print_help()
            return 2
        return cmd_tag(args)
    if cmd == "run":
        return cmd_run()
    if cmd == "seed":
        return cmd_seed()
    if cmd == "status":
        return cmd_status()
    parser.error(f"未知命令: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
