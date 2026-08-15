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
_POLITICS_KEYWORDS = ("politics", "election", "trump", "biden", "president", "senate", "congress", "house", "prime-minister", "invade", "war", "minister", "reelect", "party")
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


# ======================================================================
# 细分联赛/项目（slug 前缀 -> 具体联赛名）+ 完整分类字典（建表用）
# ======================================================================

def _norm_code(code: str) -> str:
    c = code
    while c and c[-1].isdigit():
        c = c[:-1]
    return c


_CODE_LEAGUE = {
    "lal": "西甲", "lal2": "西乙", "clf": "意甲", "bel": "比利时甲",
    "bel1": "比利时甲", "bra": "巴甲", "bra2": "巴乙", "bra3": "巴丙",
    "tur": "土超", "arg": "阿甲", "argpn": "阿根廷职业联",
    "ere": "荷甲", "spl": "苏超", "itc": "意乙", "pol": "波兰甲",
    "por": "葡超", "rus": "俄超", "rou": "罗马尼亚甲", "rou1": "罗马尼亚甲",
    "saf": "南非超", "saf1": "南非超", "srb": "塞尔维亚超", "aut": "奥甲",
    "hr": "克罗地亚甲", "hr1": "克罗地亚甲", "fin": "芬超", "fin1": "芬超",
    "es": "西甲", "es2": "西乙", "chl": "智利甲", "chl2": "智利乙",
    "mls": "美职联", "usl": "USL", "uslc": "USLC", "bl": "德乙",
    "bl2": "德乙", "frtc": "法国杯", "ncaa": "大学足球",
    "mlb": "MLB", "nfl": "NFL", "wnba": "WNBA",
    "atp": "ATP", "wta": "WTA",
    "lol": "英雄联盟", "cs": "CS", "cs2": "CS2", "val": "瓦罗兰特",
    "ufc": "UFC",
}

_KEYWORD_LEAGUE = {
    "加密": ("bitcoin", "btc", "ethereum", "eth", "xrp", "solana", "crypto", "doge", "price-of-", "biden"),
    "政治": ("politics", "election", "trump", "president", "senate", "congress", "house", "prime-minister", "invade"),
    "娱乐": ("oscar", "movie", "film", "box-office", "grammy", "spotify"),
}


def slug_to_league(slug):
    slug = (slug or "").strip().lower()
    if not slug:
        return None
    code = slug.split("-", 1)[0] if slug else ""
    norm = _norm_code(code)
    if norm and norm in _CODE_LEAGUE:
        return _CODE_LEAGUE[norm]
    for league, kws in _KEYWORD_LEAGUE.items():
        if any(k in slug for k in kws):
            return league
    return None


def classify_slug(slug):
    cat = slug_to_sport(slug)
    league = slug_to_league(slug)
    return cat, league


def market_dict():
    from collections import defaultdict
    cat_codes = defaultdict(set)
    for code, sport in _CODE_SPORT.items():
        cat_codes[sport].add(code)
    out = []
    order = 0
    for cat, codes in sorted(cat_codes.items(), key=lambda kv: list(MARKET_EMOJI.keys()).index(kv[0]) if kv[0] in MARKET_EMOJI else 99):
        out.append({"level": "category", "prefix": ",".join(sorted(codes)),
                    "category": cat, "league": "", "emoji": MARKET_EMOJI.get(cat, ""), "ord": order})
        order += 1
    for code, league in _CODE_LEAGUE.items():
        sport = _CODE_SPORT.get(_norm_code(code))
        out.append({"level": "league", "prefix": code,
                    "category": sport or "其他", "league": league,
                    "emoji": MARKET_EMOJI.get(sport or "", ""), "ord": order})
        order += 1
    return out


WALLET_SOURCE_TYPES = {
    "lb": "排行榜",
    "community": "社区推荐",
    "smallcap": "小资金发现",
    "manual": "手动关注",
}
