"""
聪明钱名单构建：排行榜播种 → 准入过滤 → 做市商剔除 → 0-100 评分。

流程（借鉴 kydlikebtc/polymarket-whalewatch 的设计，Python 实现）：
    1. 播种：日/周/月排行榜各取 top N（含分类榜），按地址去重合并
    2. 准入：胜率 ≥ 55%（closed ≥ 10 笔才算数）且 周期 PnL ≥ min_pnl
    3. 剔除做市商：1h 窗口内成交 ≥ 20 笔且买卖双向活跃 → MM，不要
    4. 评分：利润 40 + 资金效率 30 + 胜率 30，按分排序取前 max_wallets
    5. extra_addresses 手动追加（跳过准入，仍参与剔除）
"""
import logging
import math
import time
from dataclasses import dataclass, field

from src.api import data_api
from src.config import SmartMoneyConfig
from src.smart import watchlist

logger = logging.getLogger(__name__)


@dataclass
class Wallet:
    address: str
    name: str = ""
    pnl: float = 0.0
    volume: float = 0.0
    win_rate: float | None = None
    profit_factor: float | None = None
    closed_count: int = 0
    score: float = 0.0
    source: str = ""
    is_mm: bool = False
    reason: str = ""  # 淘汰原因（调试用）
    extra: dict = field(default_factory=dict)


# ======================================================================
# 评分（whalewatch 公式：利润 40 + 资金效率 30 + 胜率 30）
# ======================================================================

def score_wallet(w: Wallet) -> float:
    # 利润分：$1k 起步，$100k 封顶，log 缩放
    pnl_s = 0.0
    if w.pnl > 0:
        pnl_s = 40.0 * min(math.log10(w.pnl / 1000.0) / 2.0, 1.0) if w.pnl >= 1000 else 0.0
    # 资金效率分：PnL / volume，≥ 30% 封顶
    eff_s = 0.0
    if w.volume > 0:
        eff_s = 30.0 * min(max(w.pnl / w.volume, 0.0) / 0.30, 1.0)
    # 胜率分：55% 起步，75% 满分，线性
    wr_s = 0.0
    if w.win_rate is not None:
        wr_s = 30.0 * min(max((w.win_rate - 0.55) / 0.20, 0.0), 1.0)
    return round(pnl_s + eff_s + wr_s, 1)


# ======================================================================
# 做市商检测
# ======================================================================

def looks_like_market_maker(address: str, cfg: SmartMoneyConfig) -> bool:
    """1h 窗口内成交笔数多且买卖双向都活跃 → 做市商。"""
    since = int(time.time()) - cfg.mm_window_sec
    acts = data_api.fetch_activity(address, limit=200, start_ts=since)
    if len(acts) < cfg.mm_min_trades:
        return False
    trades = [a for a in acts if a["side"] in ("BUY", "SELL")]
    buys = sum(1 for a in trades if a["side"] == "BUY")
    sells = len(trades) - buys
    if len(trades) < cfg.mm_min_trades or buys == 0 or sells == 0:
        return False
    # 双向均衡（0.3 ~ 3.3 之间）且高频 → MM
    ratio = buys / sells
    return 0.3 <= ratio <= 3.3


# ======================================================================
# 名单构建
# ======================================================================

def _seed_from_leaderboards(cfg: SmartMoneyConfig) -> dict[str, Wallet]:
    """拉取所有排行榜，按地址去重合并，保留各榜最优 PnL。"""
    candidates: dict[str, Wallet] = {}
    for period in cfg.seed_periods:
        for category in cfg.seed_categories:
            try:
                entries = data_api.fetch_leaderboard(
                    period=period, limit=cfg.seed_per_period, category=category)
            except Exception as e:
                logger.warning("排行榜拉取失败 %s/%s: %s", period, category, e)
                continue
            src = f"lb:{period}:{category}"
            for e in entries:
                addr = e["address"]
                if not addr:
                    continue
                cur = candidates.get(addr)
                if cur is None:
                    w = Wallet(
                        address=addr,
                        name=e["userName"] or (e["xUsername"] or ""),
                        pnl=e["pnl"],
                        volume=e["volume"],
                        source=src,
                    )
                    candidates[addr] = w
                else:
                    # 多榜命中：取最优 PnL，来源累记
                    if e["pnl"] > cur.pnl:
                        cur.pnl = e["pnl"]
                        cur.volume = max(cur.volume, e["volume"])
                    cur.source = f"{cur.source},{src}" if src not in cur.source else cur.source
            time.sleep(0.2)
    return candidates


def _source_label(src: str) -> str:
    return {
        "x": "X/Twitter", "reddit": "Reddit", "manual": "手动关注",
        "custom": "自定义", "community": "社区推荐", "smallcap": "小资金聪明钱",
    }.get(src, src or "社区推荐")


def build_watchlist(cfg: SmartMoneyConfig) -> tuple[list[Wallet], list[Wallet]]:
    """构建监控名单。

    Returns:
        (watchlist, rejected)：入围名单 + 被淘汰名单（含淘汰原因，供调试/通知）
    """
    candidates = _seed_from_leaderboards(cfg)
    logger.info("播种完成：%d 个候选地址", len(candidates))

    # 手动追加（.env SM_EXTRA_ADDRESSES）
    for addr in cfg.extra_addresses:
        addr = addr.lower()
        if addr and addr not in candidates:
            candidates[addr] = Wallet(address=addr, source="manual")

    # 社区/手动推荐钱包（data/watchlist.json）
    try:
        for rec in watchlist.active(cfg.watchlist_path):
            addr = rec["address"]
            src = rec.get("source") or "community"
            note = rec.get("note") or ""
            if addr and addr not in candidates:
                w = Wallet(address=addr, source="community:" + src)
                w.extra = {"note": note, "source_label": _source_label(src)}
                candidates[addr] = w
            elif addr in candidates and candidates[addr].source == "manual":
                candidates[addr].source = "community:" + src
                candidates[addr].extra.setdefault("note", note)
                candidates[addr].extra["source_label"] = _source_label(src)
    except Exception as e:
        logger.warning("加载 watchlist.json 失败: %s", e)

    rejected: list[Wallet] = []

    # 准入 + 剔除
    passed: list[Wallet] = []
    for w in candidates.values():
        if w.source == "manual" or w.source.startswith("community:"):
            w.reason = ""
            passed.append(w)
            continue
        if w.pnl < cfg.min_pnl:
            w.reason = f"pnl<{cfg.min_pnl:.0f}"
            rejected.append(w)
            continue
        stats = data_api.wallet_stats(w.address)
        w.win_rate = stats["win_rate"]
        w.profit_factor = stats["profit_factor"]
        w.closed_count = stats["closed_count"]
        if w.closed_count < cfg.min_closed_trades or (
            w.win_rate is not None and w.win_rate < cfg.min_win_rate
        ):
            w.reason = (
                f"win_rate={(w.win_rate or 0):.0%}/closed={w.closed_count}"
            )
            rejected.append(w)
            continue
        if looks_like_market_maker(w.address, cfg):
            w.is_mm = True
            w.reason = "market-maker"
            rejected.append(w)
            continue
        passed.append(w)

    for w in passed:
        w.score = score_wallet(w)
    passed.sort(key=lambda x: x.score, reverse=True)

    if len(passed) > cfg.max_wallets:
        overflow = passed[cfg.max_wallets:]
        for w in overflow:
            w.reason = f"overflow(score={w.score})"
        rejected.extend(overflow)
        passed = passed[:cfg.max_wallets]

    logger.info("准入 %d 个（淘汰 %d 个）", len(passed), len(rejected))
    return passed, rejected
