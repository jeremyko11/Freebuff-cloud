"""钱包市场分类标签：基于历史信号 slug 推断钱包主要活跃的市场/运动类型。

slug 前缀通常是 Polymarket 联赛/运动代码（MLB/NFL/ATP/UFC/LOL/CS 等）。
此模块提供：
  - slug_to_sport(slug): 单条 slug -> 运动类型
  - wallet_market_type(slugs): 钱包全部信号 slug -> 主导市场类型
  - MARKET_EMOJI: 运动类型 -> emoji
"""

# 已知运动代码前缀 -> 运动类型（slug 以 xx- 开头，取 '-' 前段）
_CODE_SPORT = {
    # 足球（各国联赛代码 + 部分带数字）
    "lal": "足球", "clf": "足球", "bel": "足球", "bel1": "足球", "bra": "足球",
    "bra2": "足球", "bra3": "足球", "tur": "足球", "arg": "足球", "argpn": "足球",
    "ere": "足球", "spl": "足球", "itc": "足球", "pol": "足球", "por": "足球",
    "rus": "足球", "rou": "足球", "rou1": "足球", "saf": "足球", "saf1": "足球",
    "srb": "足球", "aut": "足球", "hr": "足球", "hr1": "足球", "fin": "足球",
    "fin1": "足球", "es": "足球", "es2": "足球", "chl": "足球", "chl2": "足球",
    "mls": "足球", "usl": "足球", "uslc": "足球", "bl": "足球", "bl2": "足球",
    "frtc": "足球", "ncaa": "足球",
    # 棒球
    "mlb": "棒球",
    # 橄榄球
    "nfl": "橄榄球",
    # 篮球
    "wnba": "篮球", "wnb": "篮球",
    # 网球
    "atp": "网球", "wta": "网球",
    # 电竞
    "lol": "电竞", "cs": "电竞", "cs2": "电竞", "val": "电竞",
    # 搏击
    "ufc": "搏击",
}

# 加密 / 其他市场的关键词（slug 全词匹配）
_CRYPTO_KEYWORDS = ("bitcoin", "btc", "ethereum", "eth", "xrp", "solana", "crypto", "doge")
_POLITICS_KEYWORDS = ("politics", "election", "trump", "biden", "president", "senate", "congress", "house")
_OTHER_KEYWORDS = ("will", "the-price-of-")

MARKET_EMOJI = {
    "足球": "⚽", "棒球": "⚾", "橄榄球": "🏈", "篮球": "🏀",
    "网球": "🎾", "电竞": "🎮", "搏击": "🥊", "加密": "🪙",
    "政治": "🗳", "综合/其他": "📊",
}


def slug_to_sport(slug: str) -> str | None:
    """单条 slug -> 市场类型（体育大类/加密/政治），无法识别返回 None。"""
    slug = (slug or "").strip().lower()
    if not slug:
        return None
    code = slug.split("-", 1)[0] if slug else ""
    if code and code in _CODE_SPORT:
        return _CODE_SPORT[code]
    # 加密 / 政治：关键词命中
    if any(k in slug for k in _CRYPTO_KEYWORDS):
        return "加密"
    if any(k in slug for k in _POLITICS_KEYWORDS):
        return "政治"
    return None


def wallet_market_type(slugs: list[str]) -> str | None:
    """钱包全部信号的 slug -> 主导市场类型（出现最多的）。空/无法识别返回 None。"""
    from collections import Counter
    counter: Counter = Counter()
    for slug in slugs:
        s = slug_to_sport(slug)
        if s:
            counter[s] += 1
    if not counter:
        return None
    # 同票时按预定义顺序取靠前者（稳定性）
    order = {"足球": 0, "棒球": 1, "橄榄球": 2, "篮球": 3, "网球": 4,
             "电竞": 5, "搏击": 6, "加密": 7, "政治": 8, "综合/其他": 9}
    top = max(counter.items(), key=lambda kv: (kv[1], -order.get(kv[0], 99)))
    return top[0]


def market_emoji(market: str | None) -> str:
    if not market:
        return ""
    return MARKET_EMOJI.get(market, "")


def market_label(market: str | None) -> str:
    """市场类型 -> 展示文本（emoji + 中文）。None 返回空。"""
    if not market:
        return ""
    return f"{market_emoji(market)} {market}".strip()
