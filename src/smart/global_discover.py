"""Polymarket 全局活跃交易者发现。

从官方 data-api 全局成交流采样大量活跃钱包，用盈利度/活跃度筛选，
发现排行榜之外持续活跃且有盈利的交易者——补充进观察名单。

这是最大的信号池：不限排行榜，抓"正在动、且赚钱"的聪明钱。
"""
import logging
import time

from src.api import data_api

logger = logging.getLogger(__name__)


def sample_global_wallets(sample_pages: int = 6, per_page: int = 300) -> dict:
    """从全局成交流采样活跃钱包。返回 {address: {name, usdc}}。"""
    wallets = {}
    for _ in range(sample_pages):
        try:
            trades = data_api.fetch_trades_global(limit=per_page)
        except Exception:
            break
        for t in trades:
            w = (t.get("proxyWallet") or "").lower()
            if not w:
                continue
            usdc = (t.get("size") or 0) * (t.get("price") or 0)
            if w in wallets:
                wallets[w]["usdc"] += usdc
            else:
                wallets[w] = {"name": t.get("name") or "", "usdc": usdc}
        time.sleep(0.2)
    return wallets


def discover_global_smart(budget: int = 40, existing: set = None, sample_pages: int = 6) -> list[dict]:
    """发现全局活跃盈利交易者。existing=已知地址(跳过)。"""
    wallets = sample_global_wallets(sample_pages)
    existing = existing or set()
    # 过滤已知，优先大额
    pool = [(a, i) for a, i in wallets.items() if a not in existing]
    pool.sort(key=lambda x: x[1]["usdc"], reverse=True)
    pool = pool[:max(budget * 3, 30)]  # 候选是预算3倍

    candidates = []
    evaluated = 0
    for addr, info in pool:
        if evaluated >= budget:
            break
        evaluated += 1
        try:
            m = data_api.wallet_profit_metrics(addr)
        except Exception:
            continue
        # 保质量：只纳入已实现盈利 >0 的（"聪明钱"= 盈利且活跃）
        vol = m["volume"]
        rpnl = m["realized_pnl"]
        if rpnl > 0 and vol > 0:
            candidates.append({
                "address": addr,
                "name": info["name"],
                "realized_pnl": rpnl,
                "volume": vol,
                "activity_usdc": info["usdc"],
            })
    candidates.sort(key=lambda c: c["realized_pnl"], reverse=True)
    logger.info("全局发现：采样 %d 钱包，评估 %d，筛得 %d 个盈利者",
                len(wallets), evaluated, len(candidates))
    return candidates[:budget]
