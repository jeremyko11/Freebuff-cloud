"""LunarCrush 社交脉搏信号。

用 LunarCrush 免费 topic 接口监控 Polymarket 相关话题的社交热度/情绪
（X/Reddit/TikTok/YouTube 提及数、情绪分、趋势），热度突增或情绪激变
时产生社交信号。作为聪明钱信号的补充维度。

接口：lunarcrush.com/api4/public/topic/<keyword>/v1（免费）
"""
import json
import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

API = "https://lunarcrush.com/api4/public/topic"
DEFAULT_TOPICS = [
    "polymarket", "prediction market",
    "bitcoin", "ethereum",
    "trump", "us election", "iran", "war",
    "interest rate", "fed", "recession",
    "hurricane", "yangzhou",
]
_STATE_FILE = "data/social_pulse_state.json"


def fetch_topic(keyword: str) -> dict:
    import urllib.parse
    url = f"{API}/{urllib.parse.quote(keyword)}/v1"
    try:
        r = requests.get(url, headers={"Authorization": "Bearer x"}, timeout=12)
        if r.status_code != 200:
            return {}
        return r.json().get("data", {})
    except Exception as e:
        logger.warning("LunarCrush 拉取 %s 失败: %s", keyword, e)
        return {}


def load_state() -> dict:
    try:
        return json.loads(Path(_STATE_FILE).read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    Path(_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(_STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _social_score(data: dict) -> dict:
    cnt = data.get("types_count") or {}
    sent = data.get("types_sentiment") or {}
    total = sum(v for v in cnt.values() if isinstance(v, (int, float)))
    sents = [v for v in sent.values() if isinstance(v, (int, float))]
    avg_sent = round(sum(sents) / len(sents)) if sents else 50
    return {"posts": total, "tweets": cnt.get("tweet", 0),
            "reddit": cnt.get("reddit-post", 0), "sentiment": avg_sent,
            "trend": data.get("trend", ""), "rank": data.get("topic_rank")}


def check_social_signals(topics: list[str] = None) -> list[dict]:
    topics = topics or DEFAULT_TOPICS
    state = load_state()
    signals = []
    for kw in topics:
        data = fetch_topic(kw)
        if not data:
            continue
        cur = _social_score(data)
        prev = state.get(kw, {})
        if prev:
            posts_delta = (cur["posts"] or 0) - (prev.get("posts") or 0)
            sent_delta = (cur["sentiment"] or 50) - (prev.get("sentiment") or 50)
            # 趋势转向（非flat波动）也是信号
            trend_shift = prev.get("trend") != cur["trend"] and cur["trend"] in ("up", "down")
            if posts_delta > max(500, prev.get("posts") or 0):
                signals.append({"keyword": kw, "type": "热度突增", "posts": cur["posts"],
                                "delta": posts_delta, "trend": cur["trend"]})
            elif abs(sent_delta) > 20:
                signals.append({"keyword": kw, "type": "情绪突变", "sentiment": cur["sentiment"],
                                "delta": sent_delta})
            elif trend_shift:
                signals.append({"keyword": kw, "type": "趋势转向", "trend": cur["trend"],
                                "prev_trend": prev.get("trend"), "sentiment": cur["sentiment"]})
        state[kw] = cur
    save_state(state)
    return signals


GAMMA_API = "https://gamma-api.polymarket.com/markets"
_mkt_cache = {"ts": 0, "markets": {}}
_MKT_TTL = 300  # 5 分钟缓存
_KEYWORD_TAG = {
    "bitcoin": "crypto", "ethereum": "crypto", "polymarket": "crypto",
    "prediction market": "crypto", "interest rate": "economics",
    "fed": "economics", "recession": "economics", "iran": "geopolitics",
    "war": "geopolitics", "hurricane": "weather", "yangzhou": "politics",
    "trump": "us-elections", "us election": "us-elections",
}


def _keyword_tokens(kw: str) -> list[str]:
    """把话题关键词映射为市场标题检索 token（bitcoin->btc 等）。"""
    aliases = {
        "bitcoin": ["bitcoin", "btc"],
        "ethereum": ["ethereum", "eth"],
        "polymarket": ["polymarket"],
        "prediction market": ["prediction"],
        "trump": ["trump"],
        "us election": ["election"],
        "iran": ["iran"],
        "war": ["war"],
        "interest rate": ["interest rate", "fed rate"],
        "fed": ["fed", "rate"],
        "recession": ["recession"],
        "hurricane": ["hurricane", "storm"],
        "yangzhou": ["yangzhou"],
    }
    return aliases.get(kw.lower(), [kw.lower()])


def _fetch_open_markets(tag: str) -> list[dict]:
    """拉某 tag 在盘高成交量市场（带缓存），返回轻量 dict 列表。"""
    import urllib.parse
    key = tag or "all"
    now = time.time()
    cached = _mkt_cache["markets"].get(key)
    if cached and now - _mkt_cache["ts"] < _MKT_TTL:
        return cached
    params = {"closed": "false", "order": "volume",
              "ascending": "false", "limit": 100}
    if tag:
        params["tag_slug"] = tag
    try:
        qs = urllib.parse.urlencode(params)
        r = requests.get(f"{GAMMA_API}?{qs}",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        rows = r.json() if r.status_code == 200 else []
        out = []
        for m in rows:
            if not m.get("question"):
                continue
            try:
                vol = float(m.get("volume") or 0)
            except (TypeError, ValueError):
                vol = 0
            try:
                price = float((m.get("outcomePrices") or [0.5, 0.5])[0])
            except (TypeError, ValueError):
                price = 0.5
            out.append({"question": m["question"], "slug": m.get("slug"),
                        "volume": vol, "price": price})
        _mkt_cache["markets"][key] = out
        _mkt_cache["ts"] = now
        return out
    except Exception as e:
        logger.warning("gamma 拉市场失败: %s", e)
        return []


def find_related_markets(keyword: str, n: int = 3) -> list[dict]:
    """按话题关键词，从在盘市场中挑标题命中的，返回 n 个（带缓存）。"""
    tokens = _keyword_tokens(keyword)
    if not tokens:
        return []
    tag = _KEYWORD_TAG.get(keyword.lower())
    # 先取专用 tag 的市场池，再补通用池，合并后按 token 命中
    pool = _fetch_open_markets(tag) + _fetch_open_markets(None)
    seen = set()
    hits = []
    for m in pool:
        if m["slug"] in seen:
            continue
        seen.add(m["slug"])
        import re
        q = (m.get("question") or "").lower()
        hit = False
        for t in tokens:
            if re.search(r"\b" + re.escape(t) + r"\b", q):
                hit = True
                break
        if hit:
            hits.append(m)
    hits.sort(key=lambda h: h["volume"], reverse=True)
    return hits[:n]


def format_social_signal(sig: dict) -> str:
    if sig.get("type") == "热度突增":
        return (f"🔥 <b>[社交热度突增]</b> {sig['keyword']}\n提及 {sig['posts']:,} "
                f"(+{sig['delta']:,}) 趋势:{sig.get('trend','-')}")
    if sig.get("type") == "趋势转向":
        arrow = "📈" if sig.get("trend")=="up" else "📉"
        return (f"{arrow} <b>[社交趋势转向]</b> {sig['keyword']}\n{sig.get('prev_trend')}→{sig.get('trend')} 情绪 {sig.get('sentiment')}")
    return (f"🌡 <b>[社交情绪突变]</b> {sig['keyword']}\n情绪 {sig['sentiment']} (变化 {sig['delta']:+})")
