#!/usr/bin/env python3
"""为附加源钱包（天气/全局/小盘/社区/HN）回填真实方向胜率。

复用 verify_signals 的 gamma 结算查询：对该来源钱包的全部已推送信号
做方向验证，按钱包聚合出胜率，回写 wallets.win_rate / score。

用法：python scripts/verify_wallets.py [--hours 720] [--dry-run]
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import verify_signals as VS  # 复用 gamma_by_slug / resolve_outcome_index

_REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    import os
    if os.getcwd() != str(_REPO):
        os.chdir(_REPO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=720)  # 默认近30天
    ap.add_argument("--sources", default="天气,全局,小盘,社区,手动,HackerNews")
    ap.add_argument("--dry-run", action="store_true", help="只统计不回写")
    ap.add_argument("--report", action="store_true", help="日报模式：紧凑摘要")
    ap.add_argument("--db", default="data/freebuff.db")
    ap.add_argument("--out", default="data/wallet_verify.json")
    args = ap.parse_args()

    wanted = {x.strip() for x in args.sources.split(",") if x.strip()}

    def src_type(s: str) -> str:
        if not s:
            return "排行榜"
        if s.startswith("community:smallcap"):
            return "小盘"
        if s.startswith("community:weather"):
            return "天气"
        if s.startswith("community:global"):
            return "全局"
        if s.startswith("community:hn"):
            return "HackerNews"
        if s.startswith("community:"):
            return "社区"
        if s == "manual":
            return "手动"
        return "排行榜"

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    since = time.time() - args.hours * 3600

    # 目标附加源钱包
    wrows = db.execute(
        "SELECT address, name, source FROM wallets WHERE active=1").fetchall()
    targets = {}
    for w in wrows:
        st = src_type(w["source"])
        if st in wanted:
            targets[w["address"].lower()] = {"name": w["name"], "src": st,
                                             "source_raw": w["source"], "signals": []}
    print(f"目标附加钱包: {len(targets)} 个（来源 {sorted(wanted)}）")

    # 拉它们的已推送信号
    sig_rows = db.execute(
        """SELECT id, address, slug, outcome, price AS buy_price, type, wallet_name,
                  market_category
           FROM signals
           WHERE notified=1 AND slug IS NOT NULL AND slug<>''
             AND created_at>=? AND address IS NOT NULL""",
        (since,)).fetchall()
    for r in sig_rows:
        t = targets.get((r["address"] or "").lower())
        if t:
            t["signals"].append(r)
    # 过滤无信号的钱包
    targets = {a: t for a, t in targets.items() if t["signals"]}
    print(f"有历史信号可验证的: {len(targets)} 个")

    # 并发预取 gamma
    cache: dict = {}
    unique_slugs = list(dict.fromkeys(r["slug"] for t in targets.values()
                                      for r in t["signals"]))
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(VS.gamma_by_slug, s, cache): s for s in unique_slugs}
        for f in futs:
            f.result()

    results = {}
    for addr, t in targets.items():
        n = wins = 0
        settled_wins = seen_settled = 0
        for r in t["signals"]:
            m = VS.gamma_by_slug(r["slug"], cache)
            if not m:
                continue
            idx = VS.resolve_outcome_index(r, m)
            if idx is None or idx >= len(m["_prices"]):
                continue
            cur = m["_prices"][idx]
            buy = r["buy_price"] or 0.5
            profit = cur - buy
            n += 1
            if profit > 0:
                wins += 1
            if m.get("closed"):
                seen_settled += 1
                if cur >= 0.5:
                    settled_wins += 1
        if n == 0:
            continue
        wr = wins / n
        results[addr] = {
            "name": t["name"], "src": t["src"], "signals_verified": n,
            "direction_wr": round(wr, 4),
            "settled": seen_settled,
            "settled_wr": round(settled_wins / seen_settled, 4) if seen_settled else None,
        }
        if not args.dry_run:
            score = round(wr * 100, 1)
            db.execute(
                "UPDATE wallets SET win_rate=?, score=?, updated_at=? WHERE address=?",
                (wr, score, time.time(), addr))
    db.commit()

    # 输出
    if args.report:
        _emit_report(results)
    else:
        print(f"\n{'钱包'.ljust(18)} {'来源':<8} {'验证数':>5} {'方向胜率':>8} {'结算数':>5} {'结算胜率':>8}")
        for addr, r in sorted(results.items(), key=lambda kv: -kv[1]["direction_wr"]):
            st = r["settled_wr"]
            print(f"{addr[:16].ljust(18)} {r['src']:<8} {r['signals_verified']:>5} "
                  f"{100*r['direction_wr']:>7.1f}% {r['settled']:>5} "
                  f"{('%.1f%%' % (100*st)) if st is not None else '-':>8}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1))
    suffix = "（dry-run 未回写）" if args.dry_run else "（已回写 win_rate/score）"
    print(f"\n明细: {args.out} {suffix}")
    return 0


def _emit_report(results: dict) -> None:
    """日报模式：紧凑摘要 —— 各来源钱包数/样本/胜率均值。"""
    if not results:
        print("小盘钱包验证：无历史信号结果")
        return
    by_src = defaultdict(list)
    for addr, r in results.items():
        by_src[r["src"]].append((addr, r))
    print("======== 钱包表现验证 (附加源) ========")
    for src, rs in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
        n_w = len(rs)
        wr = sum(r["direction_wr"] for _a, r in rs) / n_w
        samples = sum(r["signals_verified"] for _a, r in rs)
        # 只显示样本>=2 的稳定钱包
        print(f"  {src}: {n_w}钱包 均方向胜率{100*wr:.0f}% 样本{samples}")
        for addr, r in sorted(rs, key=lambda x: -x[1]["direction_wr"])[:3]:
            n = r["signals_verified"]
            if n >= 2:
                nm = r["name"] or addr[:14]
                print(f"    · {nm[:20]} {100*r['direction_wr']:.0f}% (n={n})")


if __name__ == "__main__":
    sys.exit(main())
