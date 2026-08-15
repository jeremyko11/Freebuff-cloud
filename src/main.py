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


def _sign_usd(amount: float) -> str:
    """正负显式标注金额：+$X / -$X / $0。"""
    if amount > 0:
        return f"+${amount:,.0f}"
    elif amount < 0:
        return f"-${-amount:,.0f}"
    return "$0"


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
        from src.smart.market_tags import market_emoji
        auto = [t for t in (w.get("auto_tags") or "").split(",") if t]
        manual = [t for t in (w.get("manual_tags") or "").split(",") if t]
        wr = f"{w['win_rate']:.0%}" if w["win_rate"] is not None else "-"
        name = w["name"] or w["address"][:16]
        mt = w.get("market_type") or ""
        mt_s = f" {market_emoji(mt)}{mt}" if mt else ""
        print(f"  {w['score']:>5.1f} | {wr:>4} | {_sign_usd(w['pnl'] or 0):>12} | {name:<24} {_fmt_tags(auto, manual)}{mt_s}")
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


def cmd_discover(args) -> int:
    from src.smart import capacity, watchlist
    from src.config import get_config
    from src.api import data_api
    from src.store.db import Store
    from src.smart.discovery import Wallet

    cfg = get_config()
    store = Store(cfg.db_path)
    wl_path = cfg.smart.watchlist_path

    print(f"开始小资金聪明钱发现（热门市场 x{cfg.smart.cap_hot_markets}，预算 {cfg.smart.cap_sample_wallets} 钱包）...")
    found = capacity.discover_capacity(cfg.smart, cfg_rates=None)
    if not found:
        print("本轮未筛出符合条件的潜力钱包（可在 .env 调 CAP_VOLUME_MIN/MAX、CAP_SAMPLE_WALLETS）")
        return 0

    print(f"\n筛选出 {len(found)} 个潜力小资金聪明钱，写入 watchlist：")
    added = 0
    for c in found:
        src = "smallcap"
        watchlist.add(wl_path, c.address, source=src, note=f"小资金聪明钱 real+${c.realized_pnl:.0f} market={c.market}")
        # 也立即入库以便发现即推送
        w = Wallet(address=c.address, name=c.name, source="community:" + src)
        w.pnl = c.realized_pnl
        w.volume = c.volume
        w.extra = {"source_label": "小资金聪明钱", "note": c.market}
        store.upsert_wallets([w])
        try:
            store.compute_market_type(c.address)
        except Exception:
            pass
        auto, manual, _ = store.wallet_tags(c.address)
        p = f"{c.percent_pnl:.0f}%" if c.percent_pnl is not None else "-"
        print(f"  💡 +${c.realized_pnl:>8,.0f} | 量${c.volume:>8,.0f} | {p} | {c.name or c.address[:14]} | 市场:{c.market[:24]}")
        added += 1
    print(f"\n新增 {added} 个潜力钱包到观察名单（refresh 后生效；也可立即 python -m src.main seed）")
    return 0


def cmd_watch(args) -> int:
    from src.smart import watchlist
    from src.config import get_config
    from src.store.db import Store

    cfg = get_config()
    wl_path = cfg.smart.watchlist_path
    op = args.watch_op
    store = Store(cfg.db_path)

    if op == "add":
        addr = (args.address or "").strip().lower()
        if not addr or not addr.startswith("0x"):
            print("地址格式不对（需 0x...）")
            return 2
        src = args.source or "manual"
        note = args.note or ""
        is_new = watchlist.add(wl_path, addr, source=src, note=note)
        # 立即入库放观察名单并算标签/分类
        from src.smart.discovery import Wallet
        from src.api import data_api
        w = Wallet(address=addr, source="community:" + src)
        w.extra = {"note": note, "source_label": {"x":"X/Twitter","reddit":"Reddit","manual":"手动关注"}.get(src, src or "社区推荐")}
        try:
            stats = data_api.wallet_stats(addr)
            w.win_rate, w.profit_factor, w.closed_count = stats["win_rate"], stats["profit_factor"], stats["closed_count"]
        except Exception:
            pass
        store.upsert_wallets([w])
        try:
            store.compute_market_type(addr)
        except Exception:
            pass
        verb = "新增" if is_new else "已存在，更新备注"
        print(f"[{verb}] {addr}  (来源: {src}, 备注: {note or '-'})")
        auto, manual, _ = store.wallet_tags(addr)
        from src.smart.market_tags import market_label
        print("  市场分类:", market_label(store.get_market_type(addr)) or "-")
        return 0

    if op == "list":
        items = watchlist.all(wl_path)
        if not items:
            print("（推荐钱包为空）")
            return 0
        print(f"推荐钱包 {len(items)} 个：")
        for it in sorted(items, key=lambda x: x.get("added_ts", 0), reverse=True):
            act = "●" if it.get("active", True) else "○"
            note = it.get("note") or ""
            src = it.get("source") or "manual"
            ts = it.get("added_ts") or 0
            import time
            when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"
            print(f"  {act} {it['address']}  [{src}] {when}  {note}")
        return 0

    if op == "rm":
        addr = (args.address or "").strip().lower()
        ok = watchlist.remove(wl_path, addr)
        # 从数据库名单剔除（标 inactive 或删）
        if ok:
            try:
                with store._conn:
                    store._conn.execute("UPDATE wallets SET active=0 WHERE address=?", (addr,))
            except Exception:
                pass
            print(f"已移除: {addr}")
        else:
            print(f"未找到: {addr}")
        return 0 if ok else 1

    if op == "import":
        # 从文件批量导入：每行 地址[,来源][,备注]
        import os
        fpath = args.address
        if not os.path.exists(fpath):
            print(f"文件不存在: {fpath}")
            return 1
        added = 0
        with open(fpath, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [x.strip() for x in line.split(",")]
                a = parts[0].lower()
                src = parts[1] if len(parts) > 1 else "custom"
                note = parts[2] if len(parts) > 2 else ""
                if watchlist.add(wl_path, a, source=src, note=note):
                    added += 1
        print(f"批量导入完成，新增 {added} 个")
        return 0

    return 2


def cmd_report(args) -> int:
    """数据分析日报：分类汇总裁（利于收集/回溯/回测/分析）。"""
    import time
    from src.store.db import Store
    from src.config import get_config

    cfg = get_config()
    store = Store(cfg.db_path)
    conn = store._conn

    # 时间范围
    days = args.days
    since = time.time() - days * 86400
    tcond = f" AND created_at>{since:.0f}"

    print("=" * 60)
    print(f"Freebuff 数据分析报告（近 {days} 天）")
    print("=" * 60)

    # ---- 总览 ----
    tot = conn.execute(f"SELECT COUNT(*) n, COALESCE(SUM(usdc),0) usdc, COUNT(DISTINCT address) wa FROM signals WHERE 1=1{tcond}").fetchone()
    print(f"\n【总览】")
    print(f"  信号总数: {tot['n']}  投注金额: ${tot['usdc']:,.0f}  涉及钱包: {tot['wa']}")
    nw = conn.execute("SELECT COUNT(*) FROM wallets WHERE active=1").fetchone()[0]
    print(f"  当前活跃监控钱包: {nw}")

    # ---- 按市场分类 ----
    print(f"\n【市场分类】 (信号数 / 金额 / 钱包数 / OPEN·ADD·REDUCE·SWEEP)")
    rows = conn.execute(f"""
        SELECT market_category AS cat,
               COUNT(*) n, COALESCE(SUM(usdc),0) usdc, COUNT(DISTINCT address) wa,
               SUM(CASE WHEN type='OPEN' THEN 1 ELSE 0 END) op,
               SUM(CASE WHEN type='ADD' THEN 1 ELSE 0 END) ad,
               SUM(CASE WHEN type='REDUCE' THEN 1 ELSE 0 END) re,
               SUM(CASE WHEN type='SWEEP' THEN 1 ELSE 0 END) sw
        FROM signals WHERE market_category IS NOT NULL AND market_category != ''{tcond}
        GROUP BY market_category ORDER BY n DESC""").fetchall()
    for r in rows:
        print(f"  {r['cat']:<8} {r['n']:>5}  ${r['usdc']:>10,.0f}  {r['wa']:>3}钱包  O{r['op']}·A{r['ad']}·R{r['re']}·S{r['sw']}")
    unk = conn.execute(f"SELECT COUNT(*) FROM signals WHERE (market_category IS NULL OR market_category=''){tcond}").fetchone()[0]
    if unk: print(f"  (未分类: {unk})")

    # ---- 按来源 ----
    print(f"\n【来源分类】")
    srcs = conn.execute(f"""
        SELECT wallet_source_type src, COUNT(*) n, COALESCE(SUM(usdc),0) usdc, COUNT(DISTINCT address) wa
        FROM signals WHERE wallet_source_type IS NOT NULL AND wallet_source_type != ''{tcond}
        GROUP BY wallet_source_type ORDER BY n DESC""").fetchall()
    if not srcs:
        print("  (无来源分类数据)")
    for r in srcs:
        print(f"  {r['src']:<10} {r['n']:>5}  ${r['usdc']:>10,.0f}  {r['wa']}钱包")

    # ---- 信号类型 ----
    print(f"\n【信号类型】")
    types = conn.execute(f"SELECT type, COUNT(*) n, COALESCE(SUM(usdc),0) usdc FROM signals WHERE 1=1{tcond} GROUP BY type ORDER BY n DESC").fetchall()
    tmap = {"OPEN": "🟢新开仓", "ADD": "🟡加仓", "REDUCE": "🔴减仓/平仓", "SWEEP": "💸拆单"}
    for r in types:
        print(f"  {tmap.get(r['type'],r['type']):<12} {r['n']:>5}  ${r['usdc']:>10,.0f}")

    # ---- 按钱包（top）----
    print(f"\n【钱包活跃 Top {args.top}】")
    wrows = conn.execute(f"""
        SELECT s.wallet_name w, COUNT(*) n, COALESCE(SUM(s.usdc),0) usdc,
               COUNT(DISTINCT s.market_category) nc, MAX(s.market_category) top,
               (SELECT pnl FROM wallets wal WHERE wal.address=s.address) pnl
        FROM signals s WHERE 1=1{tcond}
        GROUP BY s.wallet_name ORDER BY n DESC LIMIT ?""", (args.top,)).fetchall()
    for r in wrows:
        name = r['w'] or "-"
        pnl_s = _sign_usd(r['pnl']) if r['pnl'] is not None else "-"
        print(f"  {name:<18} {r['n']:>5}信号 ${r['usdc']:>10,.0f} {pnl_s:>12}  {r['nc']}类市场 主:{r['top']}")

    # ---- 按联赛细分（top）----
    print(f"\n【联赛细分 Top 10】")
    lrows = conn.execute(f"""
        SELECT market_league lg, COUNT(*) n, COALESCE(SUM(usdc),0) usdc
        FROM signals WHERE market_league IS NOT NULL AND market_league != ''{tcond}
        GROUP BY market_league ORDER BY n DESC LIMIT 10""").fetchall()
    if lrows:
        for r in lrows:
            print(f"  {r['lg']:<14} {r['n']:>5}信号 ${r['usdc']:>10,.0f}")

    # ---- 提示 ----
    print("\n" + "=" * 60)
    print("高级查询提示：可用 SQL 直接查视图")
    print("  python -m src.main report            # 近7天")
    print("  python -m src.main report --days 1   # 近1天")
    print("  python -m src.main report --top 20   # 钱包top20")
    print("  SQL: SELECT * FROM vw_signals_by_category;")
    print("=" * 60)
    return 0


def cmd_backfill() -> int:
    """给存量信号的 asset（token id）回填，供今日盈亏/验证闭环使用。"""
    import time
    from src.store.db import Store
    from src.config import get_config
    cfg = get_config()
    store = Store(cfg.db_path)
    conn = store._conn
    # distinct condition_ids missing asset
    rows = conn.execute(
        "SELECT DISTINCT condition_id, outcome FROM signals WHERE asset='' AND condition_id!=''").fetchall()
    if not rows:
        print("没有需要回填的信号（都已带 asset）")
        return 0
    print(f"待回填 condition_id: {len(rows)} 条（可能重复）")
    done = 0
    updated = 0
    cache = {}
    for r in rows:
        cid = r["condition_id"]
        outcome = (r["outcome"] or "").strip().lower()
        if cid in cache:
            mapping = cache[cid]
        else:
            mapping = store.backfill_asset(cid)
            cache[cid] = mapping
            done += 1
            if done % 20 == 0:
                print(f"  已处理 {done} 个 condition_id...")
                time.sleep(1)
        token = mapping.get(outcome)
        if token:
            conn.execute("UPDATE signals SET asset=? WHERE condition_id=? AND asset=''",
                         (str(token), cid))
            updated += 1
        time.sleep(0.2)
    conn.commit()
    print(f"处理 {len(rows)} 条，回填 {updated} 条信号 asset")
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
    sub.add_parser("backfill", help="回填存量信号 asset（供今日盈亏/验证）")
    rep = sub.add_parser("report", help="数据分析日报（分类汇总）")
    rep.add_argument("--days", type=int, default=7, help="统计近 N 天（默认7）")
    rep.add_argument("--top", type=int, default=10, help="钱包Top N（默认10）")
    tag_p = sub.add_parser("tag", help="管理钱包标签")
    tag_sub = tag_p.add_subparsers(dest="tag_op")
    tag_list = tag_sub.add_parser("list", help="查看标签")
    tag_list.add_argument("who", nargs="?", default=None, help="钱包名或地址（可选）")
    for op in ("add", "rm", "clear"):
        p = tag_sub.add_parser(op, help=f"{op} 标签")
        p.add_argument("who", help="钱包名或地址")
        p.add_argument("tag_values", nargs="*", help="标签（可多个）")
    # discover subcommand
    dsub = sub.add_parser("discover", help="发现小资金聪明钱（热门市场参与者）")
    dsub.add_argument("--dry", action="store_true", help="只打印不写入")
    # watch subcommand
    watch_p = sub.add_parser("watch", help="管理社区/手动推荐钱包")
    watch_sub = watch_p.add_subparsers(dest="watch_op")
    wa = watch_sub.add_parser("add", help="加入推荐钱包")
    wa.add_argument("address", help="0x 地址")
    wa.add_argument("--source", default="manual", help="来源 x/reddit/manual/custom")
    wa.add_argument("--note", default="", help="备注")
    wl = watch_sub.add_parser("list", help="列出推荐钱包")
    wl.add_argument("address", nargs="?", default=None)
    wr = watch_sub.add_parser("rm", help="移除推荐钱包")
    wr.add_argument("address", help="0x 地址")
    wi = watch_sub.add_parser("import", help="从文件批量导入")
    wi.add_argument("address", help="文件路径，每行 地址[,来源][,备注]")
    args = parser.parse_args()

    cfg = get_config()
    _setup_logging(cfg.log_level)

    cmd = args.cmd or "run"
    if cmd == "discover":
        return cmd_discover(args)
    if cmd == "watch":
        if args.watch_op is None:
            watch_p.print_help()
            return 2
        return cmd_watch(args)
    if cmd == "tag":
        if args.tag_op is None:
            tag_p.print_help()
            return 2
        return cmd_tag(args)
    if cmd == "backfill":
        return cmd_backfill()
    if cmd == "run":
        return cmd_run()
    if cmd == "seed":
        return cmd_seed()
    if cmd == "report":
        return cmd_report(args)
    if cmd == "status":
        return cmd_status()
    parser.error(f"未知命令: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
