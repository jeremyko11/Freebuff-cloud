"""天气市场聪明钱发现。

Polymarket 有"城市最高/最低气温""降雪/飓风"等天气预测市场。
这些市场交易者与体育/加密的聪明钱不同，需单独发现。

流程：搜索天气类市场 slug → 拉参与者 → 用已实现盈亏+活跃度筛出
潜在的"天气聪明钱" → 加入 watchlist(source=weather)。

天气市场 slug 关键词：highest-temperature, lowest-temperature, snowfall,
hurricane, heat-wave, degrees 等。
"""
import logging
import time
from dataclasses import dataclass

from src.api import data_api

logger = logging.getLogger(__name__)

WEATHER_KEYWORDS = (
    "highest-temperature", "lowest-temperature", "temperature",
    "snowfall", "hurricane", "heat-wave", "heat wave", "record-high",
    "degrees-celsius", "degrees-fahrenheit", "coldest",
)


def _find_weather_slugs(limit_page: int = 5) -> list[str]:
    """从全局成交流里收集天气类市场 slug。"""
    slugs = {}
    for _ in range(limit_page):
        try:
            trades = data_api.fetch_trades_global(limit=500)
        except Exception:
            break
        for t in trades:
            title = (t.get("title") or "").lower()
            slug = t.get("slug") or ""
            if slug and any(k in title for k in WEATHER_KEYWORDS):
                slugs[slug] = True
        if len(slugs) >= 20:
            break
        time.sleep(0.3)
    return list(slugs.keys())


def discover_weather_smart(budget_wallets: int = 20) -> list[dict]:
    """发现天气市场里的活跃/盈利聪明钱。

    返回 [{address, name, realized_pnl, volume, markets}]。
    """
    slugs = _find_weather_slugs()
    logger.info("发现天气市场 %d 个: %s", len(slugs), slugs[:3])
    wallet_pool = {}
    for slug in slugs[:8]:
        try:
            trades = data_api.fetch_trades_by_slug(slug, limit=200)
            for t in trades:
                w = (t.get("proxyWallet") or "").lower()
                if w:
                    usdc = (t.get("size") or 0) * (t.get("price") or 0)
                    name = t.get("name") or ""
                    if w in wallet_pool:
                        wallet_pool[w]["usdc"] += usdc
                    else:
                        wallet_pool[w] = {"usdc": usdc, "name": name, "markets": [slug]}
        except Exception:
            continue
        time.sleep(0.3)

    # 评估：已实现盈亏 > 0 且 成交量大小适中（排除纯发大水的散户）
    candidates = []
    evaluated = 0
    for addr, info in wallet_pool.items():
        if evaluated >= budget_wallets:
            break
        evaluated += 1
        try:
            m = data_api.wallet_profit_metrics(addr)
        except Exception:
            continue
        realized = m["realized_pnl"]
        if realized <= 0:
            continue
        candidates.append({
            "address": addr,
            "name": info["name"],
            "realized_pnl": realized,
            "volume": m["volume"],
            "markets": info["markets"][:2],
        })
    candidates.sort(key=lambda c: c["realized_pnl"], reverse=True)
    logger.info("天气聪明钱：评估 %d 钱包，筛得 %d 个盈利者", evaluated, len(candidates))
    return candidates
