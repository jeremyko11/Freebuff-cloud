"""聪明钱包自动标签。

基于 wallets 统计字段（pnl/volume/win_rate/profit_factor/closed_count/score）
派生风格标签。自动标签每次名单刷新重算；手动标签由 `python -m src.main tag`
指定，自动刷新不覆盖。

标签以逗号分隔文本存放：auto_tags 列存自动，manual_tags 列存手动。
"""
from __future__ import annotations


def derive_auto_tags(
    pnl: float | None,
    volume: float | None,
    win_rate: float | None,
    profit_factor: float | None,
    closed_count: int | None,
    score: float | None,
) -> list[str]:
    """根据统计字段返回自动标签列表（已按顺序排列，可含 emoji 前缀）。"""
    tags: list[str] = []
    pnl = pnl or 0.0
    volume = volume or 0.0
    wr = win_rate or 0.0
    pf = profit_factor or 0.0
    cc = closed_count or 0
    sc = score or 0.0

    # 资金量级
    if volume >= 1_000_000:
        tags.append("Whale")           # 🐋 巨鲸
    elif volume >= 300_000:
        tags.append("BigMoney")        # 💰 大资金

    # 盈利能力
    if pnl >= 500_000:
        tags.append("ProfitKing")      # 👑 盈利之王
    if pf >= 3.0 and cc >= 20:
        tags.append("StableWin")       # 💎 稳赚

    # 风格
    if wr >= 0.90:
        tags.append("HighWin")         # 🎯 高胜率
    if 0.0 < pf < 1.0:
        tags.append("Aggressive")      # 🚀 激进（低收益高周转）
    if cc >= 100:
        tags.append("HighFreq")        # 🔁 高频
    if sc >= 90:
        tags.append("ToP")             # ✨ 顶分

    if not tags:
        tags.append("Tracker")         # 📋 普通跟踪
    return tags


def merge_tags(auto: list[str], manual: list[str]) -> list[str]:
    """自动+手动合并去重，返回有序列表（手动优先展示）。"""
    seen = set()
    out: list[str] = []
    for t in list(manual) + list(auto):
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out
