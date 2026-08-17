"""扩容名单构建：跑完整 build_watchlist（分页播种 + 准入 + 评分），统计并写入数据库。

用法：python scripts/expand_watchlist.py [--dry-run]
"""
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.smart.discovery import build_watchlist
from src.config import get_config
from src.store.db import Store

DRY = "--dry-run" in sys.argv


def main() -> int:
    cfg = get_config()
    store = Store(cfg.db_path)
    t0 = time.time()
    print(f"[{_now()}] 开始构建名单…", flush=True)
    passed, rejected = build_watchlist(cfg.smart, store=store)
    dt = time.time() - t0

    print(f"\n===== 结果 =====")
    print(f"入围: {len(passed)} | 淘汰: {len(rejected)} | 耗时: {dt:.0f}s", flush=True)
    reasons = Counter(w.reason for w in rejected)
    print("\n淘汰原因分布:")
    for r, n in reasons.most_common(8):
        print(f"  {r[:42]:<44} {n}")

    print("\n入围 Top 15:")
    for i, w in enumerate(passed[:15], 1):
        wr = f"{w.win_rate:.0%}" if w.win_rate is not None else "-"
        print(f"  {i:>2}. [{w.score:5.1f}分] 胜率{wr:>4} | PnL ${w.pnl:9,.0f} | {w.name or w.address[:16]}")

    srcs = Counter()
    for w in passed:
        for s in w.source.split(","):
            srcs[s] += 1
    print("\n入围来源统计:")
    for s, n in srcs.most_common(8):
        print(f"  {s:<28} {n}")

    if DRY:
        print("\n[dry-run] 未写入数据库")
        return 0

    store.upsert_wallets(passed, reset_inactive=False)  # 增量合并，不清现有活跃钱包
    n_active = store._conn.execute(
        "SELECT COUNT(*) FROM wallets WHERE active=1").fetchone()[0]
    print(f"\n✅ 已写入数据库，活跃钱包数: {n_active}", flush=True)
    return 0


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%H:%M:%S")


if __name__ == "__main__":
    sys.exit(main())
