"""HackerNews 社交发现：搜 Polymarket 聪明钱/交易讨论。

Algolia HN API 免费公开，可搜故事/评论。从标题/文本提取钱包地址
(0x...) 或 Polymarket 用户名，用于发现被社区讨论的交易者。
"""
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

HN_API = "https://hn.algolia.com/api/v1"


def search_polymarket(query: str, hits: int = 10) -> list[dict]:
    """搜索 HN 上的 Polymarket 讨论。返回 hits 列表。"""
    params = {"query": query, "hitsPerPage": hits, "tags": ""}
    try:
        r = requests.get(f"{HN_API}/search", params=params, timeout=12)
        r.raise_for_status()
        return r.json().get("hits", [])
    except Exception as e:
        logger.warning("HN 搜索失败: %s", e)
        return []


def extract_addresses(text: str) -> list[str]:
    """从文本提取 0x 开头的 Polymarket 钱包地址。"""
    return re.findall(r"0x[a-fA-F0-9]{6,}", text or "")


def discover_hn_smart(queries=("polymarket whale", "polymarket smart money",
                               "polymarket profit", "polymarket big winner"),
                      max_results: int = 8) -> list[dict]:
    """从 HN 讨论中提取钱包地址候选。"""
    seen = {}
    for q in queries:
        for hit in search_polymarket(q, hits=max_results):
            # 提取地址
            title = hit.get("title") or ""
            text = hit.get("story_text") or hit.get("comment_text") or ""
            for addr in extract_addresses(title + " " + text):
                if addr not in seen:
                    seen[addr.lower()] = {
                        "address": addr.lower(),
                        "source_title": title[:60],
                        "hn_url": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                    }
        time.sleep(0.5)
    return list(seen.values())
