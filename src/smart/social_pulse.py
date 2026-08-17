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
DEFAULT_TOPICS = ["polymarket", "polymarket whale", "prediction market"]
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
            if posts_delta > max(500, prev.get("posts") or 0):
                signals.append({"keyword": kw, "type": "热度突增", "posts": cur["posts"],
                                "delta": posts_delta, "trend": cur["trend"]})
            elif abs(sent_delta) > 20:
                signals.append({"keyword": kw, "type": "情绪突变", "sentiment": cur["sentiment"],
                                "delta": sent_delta})
        state[kw] = cur
    save_state(state)
    return signals


def format_social_signal(sig: dict) -> str:
    if sig.get("type") == "热度突增":
        return (f"🔥 <b>[社交热度突增]</b> {sig['keyword']}\n提及 {sig['posts']:,} "
                f"(+{sig['delta']:,}) 趋势:{sig.get('trend','-')}")
    return (f"🌡 <b>[社交情绪突变]</b> {sig['keyword']}\n情绪 {sig['sentiment']} (变化 {sig['delta']:+})")
