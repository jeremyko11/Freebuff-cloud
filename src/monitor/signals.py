"""
信号检测：把某钱包一段时间的 activity 转化为结构化信号。

信号类型：
    OPEN      新开仓（BUY，首次出现在该市场）
    ADD       加仓（BUY，已有持仓记录）
    REDUCE    减仓 / 平仓（SELL）
    SWEEP     拆单建仓（窗口内多笔小额同向累积超阈值，单笔都不报但总和要报）

去重：dedup_key = f"{address}:{conditionId}:{outcome}:{side}:{hour_bucket}"
     同 key 在 dedup_window 内只报一次。
"""
import logging
import time
from dataclasses import dataclass, field

from src.config import MonitorConfig
from src.store.db import Store

logger = logging.getLogger(__name__)

TRADE_TYPES = {"TRADE"}  # data-api activity type；其余（REDEEM 等）暂不报


@dataclass
class Signal:
    address: str
    wallet_name: str
    type: str            # OPEN / ADD / REDUCE / SWEEP
    side: str            # BUY / SELL
    conditionId: str
    outcome: str
    title: str
    slug: str
    usdc: float
    price: float
    trade_count: int = 1
    tx_hashes: list = field(default_factory=list)
    ts: float = 0.0
    dedup_key: str = ""
    tags: list = field(default_factory=list)  # 钱包标签（推送展示用）


def _hour_bucket(ts_ms: float) -> int:
    return int(ts_ms / 1000 // 3600)


def detect_signals(
    address: str,
    wallet_name: str,
    activities: list[dict],
    store: Store,
    cfg: MonitorConfig,
    known_condition_ids: set | None = None,
) -> list[Signal]:
    """从 activity 列表提取信号（含拆单检测与去重）。

    known_condition_ids: 该钱包此前已报过开仓的市场集合（None 则查 store）。
    """
    if known_condition_ids is None:
        known_condition_ids = store.get_wallet_markets(address)

    signals: list[Signal] = []
    now_ms = time.time() * 1000

    trades = [
        a for a in activities
        if (not TRADE_TYPES or a["type"] in TRADE_TYPES)
        and a["side"] in ("BUY", "SELL")
        and a["usdcSize"] > 0
    ]
    trades.sort(key=lambda a: a["timestamp"])

    # ---- 单笔信号（≥ min_signal_usdc） ----
    for a in trades:
        if a["usdcSize"] < cfg.min_signal_usdc:
            continue
        if a["timestamp"] < now_ms - cfg.dedup_window_sec * 1000 * 24:
            continue  # 太旧的补数不报
        if a["side"] == "SELL":
            stype = "REDUCE"
        else:
            stype = "OPEN" if a["conditionId"] not in known_condition_ids else "ADD"
        key = f"{address}:{a['conditionId']}:{a['outcome']}:{a['side']}:{_hour_bucket(a['timestamp'])}"
        if store.signal_seen(key, cfg.dedup_window_sec):
            continue
        signals.append(Signal(
            address=address,
            wallet_name=wallet_name,
            type=stype,
            side=a["side"],
            conditionId=a["conditionId"],
            outcome=a["outcome"],
            title=a["title"],
            slug=a["slug"],
            usdc=a["usdcSize"],
            price=a["price"],
            tx_hashes=[a["transactionHash"]] if a["transactionHash"] else [],
            ts=a["timestamp"],
            dedup_key=key,
        ))
        if stype == "OPEN":
            known_condition_ids.add(a["conditionId"])

    # ---- 拆单建仓检测（单笔小额，窗口累积超阈值） ----
    buckets: dict[tuple, list[dict]] = {}
    for a in trades:
        if a["usdcSize"] >= cfg.min_signal_usdc:
            continue  # 已单独报过的不进拆单池
        k = (a["conditionId"], a["outcome"], a["side"])
        buckets.setdefault(k, []).append(a)

    for (cid, outcome, side), group in buckets.items():
        group.sort(key=lambda a: a["timestamp"])
        win_start = 0
        i = 0
        while i < len(group):
            # 滑动窗口
            while group[i]["timestamp"] - group[win_start]["timestamp"] > cfg.sweep_window_sec * 1000:
                win_start += 1
            window = group[win_start:i + 1]
            total = sum(x["usdcSize"] for x in window)
            if len(window) >= cfg.sweep_min_trades and total >= cfg.sweep_min_total_usdc:
                last = window[-1]
                key = f"SWEEP:{address}:{cid}:{outcome}:{side}:{_hour_bucket(last['timestamp'])}"
                if not store.signal_seen(key, cfg.dedup_window_sec):
                    signals.append(Signal(
                        address=address,
                        wallet_name=wallet_name,
                        type="SWEEP",
                        side=side,
                        conditionId=cid,
                        outcome=outcome,
                        title=last["title"],
                        slug=last["slug"],
                        usdc=total,
                        price=last["price"],
                        trade_count=len(window),
                        tx_hashes=[x["transactionHash"] for x in window if x["transactionHash"]][:5],
                        ts=last["timestamp"],
                        dedup_key=key,
                    ))
                break  # 该市场该方向本轮只报一次
            i += 1

    signals.sort(key=lambda s: s.ts)
    return signals
