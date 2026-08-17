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

import json
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

# ---- 期望值(EV)评估 ----
# 历史胜率表：按买入价分段（来自 verify_signals 回溯验证, data/winrate_bands.json）
_EV_DEFAULTS = {
    "<0.1": 0.02, "0.1-0.25": 0.15, "0.25-0.5": 0.37,
    "0.5-0.8": 0.67, ">0.8": 0.86,
}
_MIN_PRICE = 0.10  # 低于此价的买入视为送钱区间，直接拒绝


def _load_winrate_bands() -> dict:
    """加载 data/winrate_bands.json 胜率表，失败用保守默认。"""
    try:
        from pathlib import Path
        d = json.loads(Path("data/winrate_bands.json").read_text())
        bands = d.get("bands", {})
        out = {}
        for name, info in bands.items():
            out[name] = {"wr": float(info.get("win_rate", 0)),
                         "n": int(info.get("n", 0))}
        if out:
            return out
    except Exception:
        pass
    return {name: {"wr": wr, "n": 0} for name, wr in _EV_DEFAULTS.items()}


def _band_of(price: float) -> str:
    if price < 0.10:
        return "<0.1"
    if price < 0.25:
        return "0.1-0.25"
    if price < 0.50:
        return "0.25-0.5"
    if price < 0.80:
        return "0.5-0.8"
    return ">0.8"


def ev_assess(s) -> tuple[float, str, str]:
    """期望值评估。返回 (ev_ratio, grade, note)。

    ev_ratio = win_rate * (1-p)/p - (1-win_rate)
    >0 正期望；grade: 🟢正EV / 🟡谨慎 / 🔴高风险
    """
    price = s.price or 0.5
    band = _band_of(price)
    info = _load_winrate_bands().get(band, {"wr": 0.5, "n": 0})
    wr, n = info["wr"], info["n"]
    rev = (1 - price) / max(price, 1e-6)  # 赢的收益倍率
    ev = wr * rev - (1 - wr)
    ntag = f" (样本{n})" if 0 < n < 20 else ""
    if n and n < 20:
        # 样本不足不稳，降一档提示
        if ev >= 0.15:
            grade = "🟡谨慎"
        else:
            grade = "🟡谨慎"
        note = f"期望{ev:+.2f} 样本{n}不足{ntag}"
    elif ev >= 0.05:
        grade, note = "🟢正EV", f"期望+{ev:.2f}倍{ntag}"
    elif ev >= -0.20:
        grade, note = "🟡谨慎", f"期望{ev:+.2f}{ntag}"
    else:
        grade, note = "🔴高风险", f"期望{ev:+.2f} 该价位历史胜率{wr:.0%}{ntag}"
    return ev, grade, note


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

    # 3) 送钱价硬过滤
    if (s.price or 0) < _MIN_PRICE:
        return False, f"送钱价<{_MIN_PRICE}"

    # 4) 金额门槛
    min_usdc = flt.get("min_usdc", 200)
    if (s.usdc or 0) < (min_usdc or 0):
        return False, f"低于${min_usdc}"

    # 5) 来源开关
    src = _resolve_source_type(s, store)
    src_enabled = flt.get("enabled_sources") or {}
    if src in src_enabled and not src_enabled[src]:
        return False, f"来源关闭:{src}"

    # 6) EV 硬门槛（可选：设 ev_min>0 时只推正期望）
    ev_min = flt.get("ev_min")  # 缺省=None 不硬过滤，只标注
    if ev_min is not None:
        try:
            ev, _g, _n = ev_assess(s)
            if ev < float(ev_min):
                return False, f"EV<{ev_min}({ev:+.2f})"
        except Exception:
            pass

    return True, ""


def apply_filter(s, store) -> tuple[bool, str]:
    return should_push(s, store)
