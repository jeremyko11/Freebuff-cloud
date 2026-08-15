"""小资金聪明钱发现器（聚焦热门市场参与者）。

从 gamma `/markets?order=volumeNum` 选高成交量"热门市场"，
用 `/trades?slug=` 拉参与者（含用户名+钱包），再用 `/positions` 评估盈利度，
筛出"小资金 + 好策略"的潜力聪明钱（这些不上排行榜——排行榜只有大资金）。

来源标记为 smallcap，通知里带"💡 潜力聪明钱"标识。
"""
import logging
import time
from dataclasses import dataclass

from src.api import data_api

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"


@dataclass
class CapacityCandidate:
    address: str
    name: str = ""
    realized_pnl: float = 0.0
    percent_pnl: float | None = None
    volume: float = 0.0
    n_positions: int = 0
    market: str = ""      # 出处市场 slug
    reason: str = ""


def _hot_markets(n: int = 5) -> list[str]:
    """从 gamma markets 按成交量取 top n 市场的 slug。"""
    import urllib.parse
    from src.api.data_api import _get_session
    params = urllib.parse.urlencode({
        "limit": str(n), "closed": "false",
        "order": "volumeNum", "ascending": "false",
    })
    url = f"{GAMMA_BASE}/markets?{params}"
    try:
        resp = _get_session().get(url, timeout=8)
        if resp.status_code == 429:
            return []
        resp.raise_for_status()
        data = resp.json()
        return [m["slug"] for m in data if isinstance(m, dict) and m.get("slug")]
    except Exception as e:
        logger.warning("热门市场获取失败: %s", e)
        return []


def _participants(slug: str) -> dict[str, str]:
    """某市场所有参与者（proxyWallet -> name）。"""
    out: dict[str, str] = {}
    try:
        trades = data_api.fetch_trades_by_slug(slug, limit=200)
        for t in trades:
            w = (t.get("proxyWallet") or "").lower()
            if w:
                out.setdefault(w, t.get("name") or "")
    except Exception as e:
        logger.debug("参与者 %s 失败: %s", slug, e)
    return out


def discover_capacity(cfg, max_candidates: int | None = None, cfg_rates=None) -> list[CapacityCandidate]:
    """热门市场参与者 -> 小资金好策略聪明钱。

    参数来自 SmartMoneyConfig：cap_hot_markets, cap_sample_wallets,
    cap_volume_min/max, cap_percent_min。
    """
    hot_n = getattr(cfg, "cap_hot_markets", 5)
    budget = max_candidates or getattr(cfg, "cap_sample_wallets", 20)
    vol_min = getattr(cfg, "cap_volume_min", 1000.0)
    vol_max = getattr(cfg, "cap_volume_max", 50000.0)
    pct_min = getattr(cfg, "cap_percent_min", 0.0)

    slugs = _hot_markets(hot_n)
    logger.info("热门市场: %s", slugs)
    # 收集多市场的参与者
    wallet_pool: dict[str, tuple[str, str]] = {}  # address -> (name, market)
    for slug in slugs:
        for addr, name in _participants(slug).items():
            wallet_pool.setdefault(addr, (name, slug))
        time.sleep(0.5)

    candidates: list[CapacityCandidate] = []
    evaluated = 0
    for addr, (name, market) in wallet_pool.items():
        if evaluated >= budget:
            break
        evaluated += 1
        try:
            m = data_api.wallet_profit_metrics(addr)
        except Exception:
            continue
        vol = m["volume"]
        realized = m["realized_pnl"]
        if vol < vol_min or vol > vol_max:
            continue          # 非小资金
        if realized <= 0:
            continue          # 无已实现盈利
        # percentRealizedPnl 往往失真，仅作参考，不强制门槛
        candidates.append(CapacityCandidate(
            address=addr, name=name, realized_pnl=realized,
            percent_pnl=m["percent_pnl"], volume=vol,
            n_positions=m["n_positions"], market=market,
        ))

    candidates.sort(key=lambda c: c.realized_pnl, reverse=True)
    logger.info("小资金发现：评估 %d 钱包，筛得 %d 个潜力聪明钱", evaluated, len(candidates))
    return candidates
