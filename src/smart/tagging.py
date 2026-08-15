"""聪明钱包自动标签（中文版）。

基于 wallets 统计字段（pnl/volume/win_rate/profit_factor/closed_count/score）
派生风格标签。自动标签每次名单刷新重算；手动标签由 `python -m src.main tag`
指定，自动刷新不覆盖。

标签以逗号分隔文本存放：auto_tags 列存自动，manual_tags 列存手动。
"""
from __future__ import annotations

# 旧版英文标签 -> 中文标签 迁移映射
TAG_MIGRATION = {
    "Whale": "鲸鱼",
    "BigMoney": "大资金",
    "ProfitKing": "盈利之王",
    "StableWin": "稳赚",
    "HighWin": "高胜率",
    "Aggressive": "激进",
    "HighFreq": "高频",
    "ToP": "顶级",
    "Tracker": "普通",
}

# 中文标签是否带 emoji 前缀
TAG_EMOJI = {
    "鲸鱼": "🐋",
    "大资金": "💰",
    "盈利之王": "👑",
    "稳赚": "💎",
    "高胜率": "🎯",
    "激进": "🚀",
    "高频": "🔁",
    "顶级": "✨",
    "普通": "📋",
}


def derive_auto_tags(
    pnl: float | None,
    volume: float | None,
    win_rate: float | None,
    profit_factor: float | None,
    closed_count: int | None,
    score: float | None,
) -> list[str]:
    """根据统计字段返回中文自动标签列表（已按顺序排列）。"""
    tags: list[str] = []
    pnl = pnl or 0.0
    volume = volume or 0.0
    wr = win_rate or 0.0
    pf = profit_factor or 0.0
    cc = closed_count or 0
    sc = score or 0.0

    # 资金量级
    if volume >= 1_000_000:
        tags.append("鲸鱼")          # 🐋
    elif volume >= 300_000:
        tags.append("大资金")        # 💰

    # 盈利能力
    if pnl >= 500_000:
        tags.append("盈利之王")      # 👑
    if pf >= 3.0 and cc >= 20:
        tags.append("稳赚")          # 💎

    # 风格
    if wr >= 0.90:
        tags.append("高胜率")        # 🎯
    if 0.0 < pf < 1.0:
        tags.append("激进")          # 🚀
    if cc >= 100:
        tags.append("高频")          # 🔁
    if sc >= 90:
        tags.append("顶级")          # ✨

    if not tags:
        tags.append("普通")          # 📋
    return tags


def with_emoji(tag: str) -> str:
    """给中文标签加上 emoji 前缀（若原本没有）。"""
    return f"{TAG_EMOJI.get(tag, '')}{tag}"


def display_tags(tags: list[str]) -> list[str]:
    """把标签转成展示用的带 emoji 形式。"""
    return [with_emoji(t) for t in tags]


def migrate_tag(tag: str) -> str:
    """把单个旧英文标签迁移为中文（含 emoji）。无映射则原样返回。"""
    t = tag.strip()
    if t in TAG_MIGRATION:
        return with_emoji(TAG_MIGRATION[t])
    return t


def migrate_tags_csv(csv: str) -> str:
    """迁移逗号分隔标签文本（旧英文 -> 中文带 emoji）。"""
    if not csv:
        return ""
    parts = []
    for t in csv.split(","):
        t = t.strip()
        if not t:
            continue
        m = migrate_tag(t)
        if m not in parts:
            parts.append(m)
    return ",".join(parts)


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
