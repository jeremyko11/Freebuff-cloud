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
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


# 准入阶段并发数（限流器全局排队，HTTP 并行；8 线程 ≈ 3-4 倍提速）
_STATS_WORKERS = 8


def _fetch_wallet_stats(w: Wallet) -> Wallet:
    """并行准入辅助：拉取该地址战绩（限流器线程安全，安全失败返回空统计）。"""
    try:
        stats = data_api.wallet_stats(w.address)
    except Exception:
        stats = {}
    w.win_rate = stats.get("win_rate")
    w.profit_factor = stats.get("profit_factor")
    w.closed_count = stats.get("closed_count") or 0
    return w


def _is_market_maker(w: Wallet, cfg: SmartMoneyConfig) -> bool:
    """并行准入辅助：做市商检测（异常按非 MM 处理，不阻塞名单构建）。"""
    try:
        return looks_like_market_maker(w.address, cfg)
    except Exception:
        return False


def _merge_store_stats(candidates: dict, exempt: list, store) -> None:
    """豁免钱包（社区/手动）合并数据库已有统计，保证评分公平、扩容后不被挤出。"""
    addrs = [w.address for w in exempt]
    if not addrs:
        return
    try:
        if store is None:
            from src.store.db import Store
            store = Store(os.environ.get("FB_DB_PATH", "data/freebuff.db"))
        rows = store._conn.execute(
            f"SELECT address,pnl,volume,win_rate,profit_factor,closed_count,name "
            f"FROM wallets WHERE address IN ({','.join('?' * len(addrs))})", addrs).fetchall()
        for r in rows:
            w = candidates.get(r[0])
            if w is None:
                continue
            if r[1] is not None:
                w.pnl = float(r[1] or 0)
            if r[2] is not None:
                w.volume = float(r[2] or 0)
            if r[3] is not None:
                w.win_rate = r[3]
            if r[4] is not None:
                w.profit_factor = r[4]
            if r[5] is not None:
                w.closed_count = int(r[5] or 0)
            if not w.name and r[6]:
                w.name = r[6]
    except Exception as e:
        logger.warning("合并豁免钱包统计失败: %s", e)


# ======================================================================
# 名单构建
# ======================================================================

def _seed_from_leaderboards(cfg: SmartMoneyConfig) -> dict[str, Wallet]:
    """拉取所有排行榜，按地址去重合并，保留各榜最优 PnL。"""
    candidates: dict[str, Wallet] = {}
    for period in cfg.seed_periods:
        for category in cfg.seed_categories:
            src = f"lb:{period}:{category}"
            got = 0
            # 分页拉取：每页最多 50 条，凑满 seed_per_period 或榜单翻完为止
            for offset in range(0, cfg.seed_per_period, 50):
                try:
                    entries = data_api.fetch_leaderboard(
                        period=period, limit=cfg.seed_per_period - offset,
                        category=category, offset=offset)
                except Exception as e:
                    logger.warning("排行榜拉取失败 %s: %s", src, e)
                    break
                if not entries:
                    break
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
                        if e.get("xUsername"):
                            w.extra["x_username"] = e["xUsername"]
                        candidates[addr] = w
                    else:
                        # 多榜命中：取最优 PnL，来源累记
                        if e["pnl"] > cur.pnl:
                            cur.pnl = e["pnl"]
                            cur.volume = max(cur.volume, e["volume"])
                        cur.source = f"{cur.source},{src}" if src not in cur.source else cur.source
                    got += 1
                if len(entries) < min(50, cfg.seed_per_period - offset):
                    break  # 榜单已翻完
                time.sleep(0.15)
            logger.info("播种 %s：%d 条", src, got)
    return candidates


def _source_label(src: str) -> str:
    return {
        "x": "X/Twitter", "reddit": "Reddit", "manual": "手动关注",
        "custom": "自定义", "community": "社区推荐", "smallcap": "小资金聪明钱",
    }.get(src, src or "社区推荐")


def build_watchlist(cfg: SmartMoneyConfig, store=None) -> tuple[list[Wallet], list[Wallet]]:
    """构建监控名单。

    store: 可选 Store 实例（用于给社区/手动钱包合并已有统计）；不传则按 FB_DB_PATH 自建。

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
    passed: list[Wallet] = []

    # ---- 准入（三阶段，网络密集部分并行） ----
    # 阶段1（无网络）：手动/社区直通（豁免准入+做市商，但照常拉统计评分） + PnL 门槛
    exempt: list[Wallet] = []
    need_stats: list[Wallet] = []
    for w in candidates.values():
        if w.source == "manual" or w.source.startswith("community:"):
            w.reason = ""
            exempt.append(w)
            continue
        if w.pnl < cfg.min_pnl:
            w.reason = f"pnl<{cfg.min_pnl:.0f}"
            rejected.append(w)
            continue
        need_stats.append(w)

    # 豁免钱包：合并数据库已有统计（pnl/volume），保证评分公平
    if exempt:
        _merge_store_stats(candidates, exempt, store)

    # 阶段2（并行）：战绩统计。豁免钱包也刷新统计，但不参与准入过滤
    stats_passed: list[Wallet] = []
    mm_targets: list[Wallet] = []
    targets = need_stats + exempt
    exempt_ids = {id(w) for w in exempt}
    if targets:
        logger.info("准入统计 %d 个候选（并发 %d）", len(targets), _STATS_WORKERS)
        with ThreadPoolExecutor(max_workers=_STATS_WORKERS) as ex:
            for w in ex.map(_fetch_wallet_stats, targets):
                if id(w) in exempt_ids:
                    stats_passed.append(w)      # 豁免：跳过胜率/笔数门槛
                    continue
                if w.closed_count < cfg.min_closed_trades or (
                    w.win_rate is not None and w.win_rate < cfg.min_win_rate
                ):
                    w.reason = (
                        f"win_rate={(w.win_rate or 0):.0%}/closed={w.closed_count}"
                    )
                    rejected.append(w)
                    continue
                stats_passed.append(w)
                mm_targets.append(w)
    passed.extend(stats_passed)

    # 阶段3（并行）：做市商剔除（只查排行榜候选）
    if mm_targets:
        mm_rejects: list[Wallet] = []
        with ThreadPoolExecutor(max_workers=_STATS_WORKERS) as ex:
            futs = {ex.submit(_is_market_maker, w, cfg): w for w in mm_targets}
            for fut in as_completed(futs):
                w = futs[fut]
                if fut.result():
                    w.is_mm = True
                    w.reason = "market-maker"
                    mm_rejects.append(w)
        for w in mm_rejects:
            passed.remove(w)
            rejected.append(w)

    for w in passed:
        w.score = score_wallet(w)
    passed.sort(key=lambda x: x.score, reverse=True)

    if len(passed) > cfg.max_wallets:
        # 溢出截断只砍排行榜钱包，豁免（社区/手动）始终保留
        non_exempt = [w for w in passed if id(w) not in exempt_ids]
        overflow = sorted(non_exempt, key=lambda x: x.score)[:len(passed) - cfg.max_wallets]
        overflow_ids = {id(w) for w in overflow}
        for w in overflow:
            w.reason = f"overflow(score={w.score})"
        rejected.extend(overflow)
        passed = [w for w in passed if id(w) not in overflow_ids]

    logger.info("准入 %d 个（淘汰 %d 个）", len(passed), len(rejected))
    return passed, rejected
