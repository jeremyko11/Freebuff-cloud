"""X 社交发现：搜索 Polymarket 聪明钱讨论 → 反查钱包 → 候选列表。

流程：
  1. 用 X API 搜索推文（提到 smart money/whale 等）
  2. 提取推文中 @用户名
  3. 用排行榜 userName/xUsername 反查钱包地址
  4. 返回能映射到钱包的 (用户名, 地址) 候选
"""
import logging

logger = logging.getLogger(__name__)


def build_username_map() -> dict:
    """排行榜用户名/xUsername -> 钱包地址 映射。"""
    from src.api.data_api import fetch_leaderboard
    m = {}
    for period in ("DAY", "WEEK", "MONTH", "ALL"):
        try:
            for r in fetch_leaderboard(period, 50, "PNL"):
                xu = (r.get("xUsername") or "").strip().lstrip("@").lower()
                nm = (r.get("userName") or "").strip().lower()
                if xu and xu not in m:
                    m[xu] = r["address"]
                if nm and nm not in m:
                    m[nm] = r["address"]
        except Exception:
            continue
    return m


def x_mentions_listed(cfg) -> list[dict]:
    """搜索 X 推文，返回 [{twitter_user, address, tweet_text}]（能映射到钱包的）。"""
    if not cfg.bearer:
        logger.warning("X_BEARER 未配置，无法搜索")
        return []
    from src.api.xapi import XClient
    client = XClient(cfg.bearer)
    tweets = client.search_recent(cfg.search_query, cfg.max_results)
    if not tweets:
        logger.info("X 搜索无结果")
        return []
    mentions = client.extract_mentions(tweets)
    umap = build_username_map()
    out = []
    for u in mentions:
        addr = umap.get(u)
        if addr:
            # 找提到它的推文
            txt = next((t["text"] for t in tweets if f"@{u}" in t["text"].lower()), "")
            out.append({"twitter_user": u, "address": addr, "tweet_text": txt[:120]})
    return out


def discover_x_smart(cfg, store) -> list[dict]:
    """X 发现并加入观察名单。返回新增的钱包列表。"""
    from src.smart import watchlist
    from src.smart.discovery import Wallet
    found = x_mentions_listed(cfg)
    if not found:
        logger.info("X 本轮未发现可映射钱包")
        return []
    added = 0
    for item in found:
        addr = item["address"].lower()
        # 加入 watchlist（社区来源）
        watchlist.add(cfg.smart.watchlist_path, addr, source="x",
                      note=f"X @{item['twitter_user']} 被推荐: {item['tweet_text'][:30]}")
        # 立即入库
        w = Wallet(address=addr, source="community:x")
        w.extra = {"source_label": "X/Twitter", "note": item['twitter_user']}
        store.upsert_wallets([w], reset_inactive=False)
        added += 1
    return found
