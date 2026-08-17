#!/usr/bin/env python3
"""推送信号方向回溯验证器。

对 DB 中已推送的信号，按 slug 拉取 Polymarket 当前/结算价格，
计算推送方向是否正确（BUY 押的 outcome 相对买入价是涨还是跌）。
产出：整体方向胜率、按类型/类目/金额段/价格段的胜率表。
用法：python scripts/verify_signals.py [--hours 24] [--sample N] [--unknown-only]
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

GAMMA = "https://gamma-api.polymarket.com/markets"
DB = "data/freebuff.db"


def gamma_by_slug(slug: str, cache: dict) -> dict | None:
    if slug in cache:
        return cache[slug]

    def _fetch(closed: str) -> dict | None:
        try:
            qs = urllib.parse.urlencode({"slug": slug, "closed": closed})
            req = urllib.request.Request(f"{GAMMA}?{qs}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                rows = json.load(r)
            m = rows[0] if rows else None
            if m:
                # gamma 的 outcomes/outcomePrices 是 JSON 字符串，需解析
                try:
                    m["_prices"] = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                except (TypeError, ValueError, json.JSONDecodeError):
                    m["_prices"] = []
                try:
                    m["_outcomes"] = json.loads(m.get("outcomes") or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    m["_outcomes"] = m.get("outcomes") or []
            return m
        except Exception:
            return None

    m = _fetch("false")
    if m is None:
        m = _fetch("true")
    cache[slug] = m
    return m


def resolve_outcome_index(sig: dict, m: dict) -> int | None:
    """找信号 outcome 在 market outcomes 里的下标（按问题/资产匹配）。"""
    oc = m.get("_outcomes") or []
    want = sig["outcome"]
    if not oc or not want:
        return None
    # 精确匹配
    for i, o in enumerate(oc):
        if o.strip().lower() == want.strip().lower():
            return i
    # 模糊包含（outcome 常带空格/编码差异）
    wl = want.strip().lower()
    for i, o in enumerate(oc):
        if wl in o.strip().lower() or o.strip().lower() in wl:
            return i
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--sample", type=int, default=0, help="0=全部")
    ap.add_argument("--since-id", type=int, default=0)
    ap.add_argument("--out", default="data/signal_verify.json")
    ap.add_argument("--report", action="store_true",
                    help="输出紧凑摘要(供日报追加)并重算 winrate_bands.json")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    since = time.time() - args.hours * 3600
    rows = db.execute(
        """SELECT id, created_at, address, wallet_name, type, side, outcome, title, slug,
                  usdc, price AS buy_price, market_category, wallet_source_type
           FROM signals
           WHERE notified=1 AND slug IS NOT NULL AND slug<>''
             AND created_at >= ?
             AND id > ?
           ORDER BY id""",
        (since, args.since_id),
    ).fetchall()
    if args.sample:
        rows = rows[:: max(1, len(rows) // args.sample)][: args.sample]
    print(f"待验证信号: {len(rows)}")

    cache: dict = {}
    # 并发预取 gamma 市场信息（带进程内共享缓存）
    unique_slugs = list(dict.fromkeys(r["slug"] for r in rows))
    with ThreadPoolExecutor(max_workers=8) as ex:
        # 预热缓存填充 unique_slugs（线程安全：dict 原子赋值）
        futs = {ex.submit(gamma_by_slug, s, cache): s for s in unique_slugs}
        for f in futs:
            f.result()

    results = []
    for r in rows:
        m = gamma_by_slug(r["slug"], cache)
        if not m:
            results.append({**dict(r), "status": "no_market", "profit": None})
            continue
        idx = resolve_outcome_index(r, m)
        if idx is None or idx >= len(m["_prices"]):
            results.append({**dict(r), "status": "no_outcome_match",
                            "cur_price": m["_prices"][0] if m["_prices"] else None,
                            "profit": None})
            continue
        cur = m["_prices"][idx]
        buy = r["buy_price"] or 0
        if m.get("closed"):
            # 结算：赢=1 输=0
            status = "settled"
            realized = (1.0 - buy) if cur >= 0.5 else (0.0 - buy)
        else:
            status = "live"
            realized = cur - buy
        results.append({**dict(r), "status": status,
                        "cur_price": cur, "profit": realized})

    # 汇总
    valid = [x for x in results if x["status"] in ("settled", "live")]
    wins = [x for x in valid if (x["profit"] or 0) > 0]
    loss = [x for x in valid if (x["profit"] or 0) < 0]
    flat = [x for x in valid if (x["profit"] or 0) == 0]
    settled = [x for x in valid if x["status"] == "settled"]

    def pct(a, b):
        return f"{100*a/b:.1f}%" if b else "-"

    print(f"\n======== 方向验证结果 ========")
    print(f"可验证: {len(valid)}  盈利: {len(wins)}  亏损: {len(loss)}  持平: {len(flat)}")
    print(f"方向准确率(盈利占比): {pct(len(wins), len(valid))}")
    print(f"已结算: {len(settled)}  (赢 {pct(sum(1 for x in settled if (x['profit'] or 0)>0), len(settled))})")
    total_profit = sum(x["profit"] or 0 for x in valid)
    print(f"按已持有市值估算总盈亏(点数): {total_profit:+.3f}")

    # 按类型
    print("\n-- 按类型 --")
    by_type = defaultdict(list)
    for x in valid:
        by_type[x["type"]].append(x)
    for t, xs in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        w = sum(1 for x in xs if (x["profit"] or 0) > 0)
        print(f"  {t:8s} n={len(xs):4d}  赢 {w:4d}  胜率 {pct(w, len(xs))}")

    # 按类别
    print("\n-- 按市场类目 --")
    by_cat = defaultdict(list)
    for x in valid:
        by_cat[x["market_category"] or "未分类"].append(x)
    for c, xs in sorted(by_cat.items(), key=lambda kv: -len(kv[1]))[:10]:
        w = sum(1 for x in xs if (x["profit"] or 0) > 0)
        print(f"  {c:10s} n={len(xs):4d}  赢 {w:4d}  胜率 {pct(w, len(xs))}")

    # 按买入价格段
    print("\n-- 按买入价格段 --")
    buckets = [(0, 0.1, "<0.1"), (0.1, 0.25, "0.1-0.25"), (0.25, 0.5, "0.25-0.5"),
               (0.5, 0.8, "0.5-0.8"), (0.8, 1.0, ">0.8")]
    for lo, hi, name in buckets:
        xs = [x for x in valid if lo <= (x["buy_price"] or 0.5) < hi]
        w = sum(1 for x in xs if (x["profit"] or 0) > 0)
        print(f"  {name:10s} n={len(xs):4d}  赢 {w:4d}  胜率 {pct(w, len(xs))}")

    # 按金额段
    print("\n-- 按金额段 --")
    amt = [(0, 100), (100, 500), (500, 1000), (1000, 5000), (5000, 999999999)]
    labels = ["<$100", "$100-500", "$500-1k", "$1-5k", ">$5k"]
    for (lo, hi), name in zip(amt, labels):
        xs = [x for x in valid if lo <= (x["usdc"] or 0) < hi]
        w = sum(1 for x in xs if (x["profit"] or 0) > 0)
        print(f"  {name:8s} n={len(xs):4d}  赢 {w:4d}  胜率 {pct(w, len(xs))}")

    # Top 贡献钱包
    print("\n-- 推送钱包表现 (n>=5) --")
    by_w = defaultdict(list)
    for x in valid:
        by_w[x["wallet_name"] or x["address"] or "?"].append(x)
    rows_sorted = sorted(by_w.items(), key=lambda kv: -sum(v["profit"] or 0 for v in kv[1]))
    for name, xs in rows_sorted[:15]:
        if len(xs) < 5:
            continue
        w = sum(1 for x in xs if (x["profit"] or 0) > 0)
        prof = sum(x["profit"] or 0 for x in xs)
        print(f"  {name[:22]:24s} n={len(xs):4d}  胜率 {pct(w, len(xs))}  盈亏 {prof:+.3f}")

    # 输出最差/最好信号明细
    worst = sorted(valid, key=lambda x: x["profit"] or 0)[:5]
    print("\n-- 最差 5 条 --")
    for x in worst:
        print(f"  {x['wallet_name'] or x['address'][:10]}: {x['title'][:45]} "
              f"押{x['outcome']} @{x['buy_price']:.2f} → {x['cur_price']:.2f} "
              f"({x['status']}) {x['profit']:+.2f}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\n明细已保存: {args.out}")

    if args.report:
        _emit_report(valid)
    return 0


def _emit_report(valid: list) -> None:
    """日报模式：输出紧凑摘要 + 重算 winrate_bands.json（供 filter 校准 EV）。"""
    import time as _t

    wins = [x for x in valid if (x["profit"] or 0) > 0]
    settled = [x for x in valid if x["status"] == "settled"]
    n = len(valid)
    wr_all = len(wins) / n if n else 0

    def pct(a, b):
        return f"{100*a/b:.1f}%" if b else "-"

    print("\n======== 推送方向验证 (日报) ========")
    print(f"✅ 验证 {n} 条 · 方向准确率 {pct(len(wins), n)} · "
          f"已结算 {len(settled)} (胜率 {pct(sum(1 for x in settled if (x['profit'] or 0) > 0), len(settled))})")

    # 按价格段胜率（顺便重算 winrate_bands.json）
    bands = [("<0.1", 0, 0.1), ("0.1-0.25", 0.1, 0.25), ("0.25-0.5", 0.25, 0.5),
             ("0.5-0.8", 0.5, 0.8), (">0.8", 0.8, 1.01)]
    band_stats = {}
    for name, lo, hi in bands:
        xs = [x for x in valid if lo <= (x["buy_price"] or 0.5) < hi]
        w = sum(1 for x in xs if (x["profit"] or 0) > 0)
        wr = w / len(xs) if xs else None
        band_stats[name] = {"n": len(xs), "win_rate": wr}
        if wr is not None:
            print(f"  {name:9s} n={len(xs):4d}  胜率 {pct(w, len(xs))}")

    # 市场类目表现（Top3 差）
    by_cat = defaultdict(list)
    for x in valid:
        by_cat[x["market_category"] or "未分类"].append(x)
    cats = sorted(by_cat.items(), key=lambda kv: -len(kv[1]))[:4]
    if cats:
        print("  " + " | ".join(
            f"{c}:{pct(sum(1 for x in xs if (x['profit'] or 0) > 0), len(xs))}"
            for c, xs in cats))

    # 重算 winrate_bands.json（有样本的档位写入，filter 用它校准）
    try:
        out = {"source": "daily verification", "generated_ts": _t.time(), "bands": {}}
        for name, st in band_stats.items():
            if st["win_rate"] is not None and st["n"] > 0:
                out["bands"][name] = {"n": st["n"], "win_rate": round(st["win_rate"], 4)}
        Path("data/winrate_bands.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1))
        print(f"\n胜率表已重算 → data/winrate_bands.json ({len(out['bands'])} 档)")
    except Exception as e:
        print(f"\n⚠️ 胜率表重算失败: {e}")


if __name__ == "__main__":
    sys.exit(main())