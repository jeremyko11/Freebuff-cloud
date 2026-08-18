#!/usr/bin/env python3
"""投注结果结算采集器：扫描未结算信号，回填结算结果，并推送结算通知。

- 对 signals 表中 settled=0 的信号，用 gamma 查 slug 对应市场是否已 closed
- 市场结算后：押注 outcome 结算价 1.0=赢 / 0.0=输 → 回填 settled_win/result_pnl
- 每批推送「已结算」通知到 Telegram（赢🟢/输🔴 + 盈亏）
- 用法: python scripts/settle_signals.py [--hours 168] [--push 1|0]
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.chdir(REPO)
sys.path.insert(0, str(REPO))            # 使 import src.* 可用
sys.path.insert(0, str(Path(__file__).resolve().parent))  # import verify_signals

from src.store.db import Store
from src.config import get_config
from src.notify.telegram import send_message, format_signal
import verify_signals as VS


def main() -> int:
    args = sys.argv[1:]
    push = "--push 0" not in (" " + " ".join(args))
    hours = 168
    max_push = 20  # 单次最多推送的结算通知条数，防刷屏
    for a in args:
        if a.startswith("--hours"):
            try:
                hours = int(a.split("=")[1] if "=" in a else args[args.index(a) + 1])
            except Exception:
                pass
        if a.startswith("--max-push"):
            try:
                max_push = int(a.split("=")[1] if "=" in a else args[args.index(a) + 1])
            except Exception:
                pass

    cfg = get_config()
    store = Store(cfg.db_path)
    p = store.pending_settlements(limit=400)
    if not p:
        print("无待结算信号")
        return 0
    print(f"待结算信号: {len(p)}")

    # 并发预取 gamma
    cache: dict = {}
    from concurrent.futures import ThreadPoolExecutor
    slugs = list(dict.fromkeys(r["slug"] for r in p))
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(VS.gamma_by_slug, s, cache): s for s in slugs}
        for f in futs:
            f.result()

    n_wins = n_loss = n_still = 0
    settled_msgs: list[str] = []
    for r in p:
        m = VS.gamma_by_slug(r["slug"], cache)
        if not m or not m.get("closed"):
            n_still += 1  # 仍在进行或查不到
            continue
        idx = VS.resolve_outcome_index(r, m)
        if idx is None or idx >= len(m["_prices"]):
            # 无法对准 outcome，标记已结算但不判胜负（防重复扫）
            store._conn.execute(
                "UPDATE signals SET settled=1, settled_at=? WHERE id=?",
                (time.time(), r["id"]))
            continue
        cur = m["_prices"][idx]  # 结算价：赢=1.0 输=0.0
        settle_price = cur
        win = 1 if cur >= 0.5 else 0
        result_pnl = (settle_price - (r["price"] or 0.5))  # 每单位点数盈亏
        store.set_signal_settlement(r["id"], win, settle_price, result_pnl, time.time())
        if win:
            n_wins += 1
        else:
            n_loss += 1
        if push and cfg.telegram.enabled:
            settled_msgs.append(_fmt_settle(r, win, result_pnl))

    # 推送结算通知（合并成少数几条，避免刷屏；受 max_push 限制）
    if settled_msgs and cfg.telegram.enabled:
        settled_msgs = settled_msgs[:max_push]
        for start in range(0, len(settled_msgs), 10):
            chunk = settled_msgs[start:start + 10]
            body = "🧾 信号已结算：" + "".join(m for m in chunk)
            send_message(cfg.telegram, body)
        print(f"已推送结算通知 {len(settled_msgs)} 条")

    stats = store.settlement_stats(hours)
    print(f"\n===== 本次结算 =====")
    print(f"赢: {n_wins} | 输: {n_loss} | 未结算/无法确认: {n_still}")
    print(f"累计(近{hours}h): 已结算 {stats['n']}, 胜率 {100*stats['wins']/stats['n'] if stats['n'] else 0:.1f}%, "
          f"总点数盈亏 {stats['pnl']:+.2f}")
    return 0


def _fmt_settle(r: dict, win: int, pnl: float) -> str:
    who = r.get("wallet_name") or (r.get("address") or "")[:10]
    title = (r.get("title") or "")[:48]
    emoji = "🟢" if win else "🔴"
    rl = f"赢 +{pnl:.2f}点" if win else f"输 {pnl:+.2f}点"
    return f"\n{emoji} {who} {r.get('type')} 押{r.get('outcome')} @{r.get('price'):.2f} → {rl}\n  {title}"


if __name__ == "__main__":
    sys.exit(main())
