"""真实 API 冒烟：leaderboard → activity → 评分链路（不发通知，不入库）。"""
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from src.api import data_api
from src.smart.discovery import Wallet, score_wallet

def main() -> int:
    t0 = time.time()
    lb = data_api.fetch_leaderboard(period="WEEK", limit=10)
    if not lb:
        print("FAIL: leaderboard 为空")
        return 1
    print(f"OK leaderboard: {len(lb)} 条，top1 = {lb[0]['userName'] or lb[0]['address'][:12]}… PnL ${lb[0]['pnl']:,.0f}")

    top = lb[0]["address"]
    acts = data_api.fetch_activity(top, limit=20)
    print(f"OK activity({top[:12]}…): {len(acts)} 条", end="")
    if acts:
        buys = sum(1 for a in acts if a["side"] == "BUY")
        print(f"，BUY {buys} / SELL {len(acts) - buys}，最新 {acts[0]['title'][:40]}")
    else:
        print()

    stats = data_api.wallet_stats(top)
    wr = f"{stats['win_rate']:.0%}" if stats["win_rate"] is not None else "N/A"
    print(f"OK wallet_stats: closed={stats['closed_count']} win_rate={wr} pf={stats['profit_factor']}")

    w = Wallet(address=top, name=lb[0]["userName"] or "", pnl=lb[0]["pnl"],
               volume=lb[0]["volume"], win_rate=stats["win_rate"],
               profit_factor=stats["profit_factor"], closed_count=stats["closed_count"])
    print(f"OK score: {score_wallet(w)}")

    from src.ratelimit import get_status
    print(f"OK ratelimit: data_api tokens={get_status()['data_api']['available_tokens']}")
    print(f"冒烟通过，耗时 {time.time() - t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
