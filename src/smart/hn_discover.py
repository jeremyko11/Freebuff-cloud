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


def extract_usernames(text: str) -> list[str]:
    """从文本提取 @用户名（小写去重）。"""
    seen = set()
    for m in re.finditer(r"@([A-Za-z0-9_]{2,})", text or ""):
        seen.add(m.group(1).lower())
    return list(seen)


def discover_hn_smart(queries=("polymarket whale", "polymarket smart money",
                               "polymarket profit", "polymarket big winner"),
                      max_results: int = 8, resolve_usernames: bool = True) -> list[dict]:
    """从 HN 讨论中提取候选。

    两条路径：
      1. 文本里直接出现的 0x 钱包地址
      2. resolve_usernames=True 时，把 @用户名反查排行榜（userName/xUsername）
         —— HN 讨论很少直接贴地址，但常提到交易者用户名
    """
    hits_text = []
    for q in queries:
        for hit in search_polymarket(q, hits=max_results):
            title = hit.get("title") or ""
            text = hit.get("story_text") or hit.get("comment_text") or ""
            hits_text.append({
                "title": title,
                "text": title + " " + text,
                "hn_url": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            })
        time.sleep(0.5)

    umap = {}
    if resolve_usernames:
        try:
            from src.smart.xdiscover import build_username_map
            umap = build_username_map()
            logger.info("HN 用户名反查映射：%d 个用户名", len(umap))
        except Exception as e:
            logger.warning("HN 用户名反查失败: %s", e)

    seen = {}
    for h in hits_text:
        for addr in extract_addresses(h["text"]):
            a = addr.lower()
            if a not in seen:
                seen[a] = {"address": a, "source": "0x地址",
                            "source_title": h["title"][:60], "hn_url": h["hn_url"]}
        if umap:
            for u in extract_usernames(h["text"]):
                addr = umap.get(u)
                if addr and addr not in seen:
                    seen[addr] = {"address": addr, "source": f"@{u}",
                                  "source_title": h["title"][:60], "hn_url": h["hn_url"]}
    return list(seen.values())
