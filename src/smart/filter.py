"""信号推送过滤器。

在推送出口（rtds_watcher._notify）判断是否推送。规则存 data 层 user_filters：
  {
    "blocked_markets": ["电竞","加密"],   # 屏蔽的市场分类
    "allowed_markets": [],                # 只收这些（空=全收）
    "allowed_wallets": ["0x..."],         # 只盯这些钱包（空=全部）
    "required_tags": ["鲸鱼"],             # 只收带这些标签的钱包（空=全部）
    "enabled_sources": {"排行榜":1,"社区":1,"小资金":1,"手动":1},  # 来源开关
    "min_usdc": 200
  }
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_FILTER = {
    "blocked_markets": [],
    "allowed_markets": [],
    "allowed_wallets": [],
    "required_tags": [],
    "enabled_sources": {"排行榜": 1, "社区": 1, "小资金": 1, "手动": 1},
    "min_usdc": 200,
}

NEW = {}


def _resolve_market_category(s, store) -> str | None:
    """从 signal 推断市场分类（优先 db 已存，否则用 slug 分类）。"""
    # 1) 从钱包 market_type 兜底
    try:
        cat = store.get_market_type(s.address)
        if cat:
            return cat
    except Exception:
        pass
    # 2) 用信号 slug 现场分类
    try:
        from src.smart.market_tags import classify_slug
        cat, _ = classify_slug(s.slug or "")
        if cat:
            return cat
    except Exception:
        pass
    return s.market_label or None


def _resolve_source_type(s, store) -> str:
    try:
        row = store._conn.execute(
            "SELECT source FROM wallets WHERE address=?", (s.address,)).fetchone()
        raw = (row["source"] if row else "") or ""
        if raw.startswith("community:smallcap"):
            return "小资金"
        if raw.startswith("community:"):
            return "社区"
        if raw == "manual":
            return "手动"
        return "排行榜"
    except Exception:
        return "排行榜"


def should_push(s, store, flt: dict | None = None) -> tuple[bool, str]:
    """对 signal 判断是否应推送。返回 (是否推送, 原因/说明)。"""
    if flt is None:
        flt = store.get_filter("push_filter", dict(DEFAULT_FILTER))
    if not flt:
        flt = dict(DEFAULT_FILTER)

    # 1) 市场分类
    cat = _resolve_market_category(s, store)
    if cat:
        if cat in (flt.get("blocked_markets") or []):
            return False, f"屏蔽市场:{cat}"
        allowed = flt.get("allowed_markets") or []
        if allowed and cat not in allowed:
            return False, f"非允许市场:{cat}"

    # 2) 钱包白名单 / 标签
    addr = (s.address or "").lower()
    allowed_w = [a.lower() for a in (flt.get("allowed_wallets") or [])]
    if allowed_w and addr not in allowed_w:
        return False, "非白名单钱包"
    req_tags = flt.get("required_tags") or []
    if req_tags:
        tags = s.tags or []
        # 匹配任意要求的标签
        if not any(r in (" ".join(tags)) for r in req_tags):
            return False, f"缺标签:{','.join(req_tags)}"

    # 3) 金额门槛
    min_usdc = flt.get("min_usdc", 200)
    if (s.usdc or 0) < (min_usdc or 0):
        return False, f"低于${min_usdc}"

    # 4) 来源开关
    src = _resolve_source_type(s, store)
    src_enabled = flt.get("enabled_sources") or {}
    if src in src_enabled and not src_enabled[src]:
        return False, f"来源关闭:{src}"

    return True, ""


def apply_filter(s, store) -> tuple[bool, str]:
    return should_push(s, store)
