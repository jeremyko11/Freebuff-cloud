"""
Polymarket Data API 客户端（data-api.polymarket.com）。

迁移自主 bot smart_money_client.py，改动：
    - 限流直接用 src.ratelimit（无 core 包依赖）
    - 重试内建（429 指数退避交给限流器，这里只做网络级重试）
    - 线程共享 Session + urllib3 重试

提供：leaderboard / positions / activity / closed-positions / value
"""
import logging
import time
import urllib.parse
from typing import Any, Optional

import requests

from src.ratelimit import EndpointCategory, acquire, categorize_url, handle_429

logger = logging.getLogger(__name__)

DATA_API_BASE = "https://data-api.polymarket.com"
UA = "Mozilla/5.0 (compatible; freebuff-cloud/0.1; +https://github.com/jeremyko11/Freebuff-cloud)"

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": UA})
        from urllib3.util.retry import Retry
        from requests.adapters import HTTPAdapter
        retry = Retry(
            total=2,
            backoff_factor=0.2,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        _session.mount("https://", adapter)
    return _session


def _request_json(url: str, timeout: float = 8.0, max_attempts: int = 3) -> Optional[Any]:
    """GET → JSON。自动限流 + 429 退避 + 网络重试。失败返回 None。"""
    category: EndpointCategory = categorize_url(url)
    last_err = None
    for attempt in range(max_attempts):
        acquire(category)
        try:
            resp = _get_session().get(url, timeout=timeout)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                handle_429(category, int(retry_after) if retry_after else None)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            logger.debug("DataAPI 请求失败（第%d次） %s: %s", attempt + 1, url[:80], e)
            if attempt < max_attempts - 1:
                time.sleep(0.3 * (attempt + 1))
    logger.warning("DataAPI 请求最终失败 %s: %s", url[:80], last_err)
    return None


# ======================================================================
# 归一化
# ======================================================================

def _ts_ms(raw) -> float:
    ts = raw.get("timestamp")
    if isinstance(ts, (int, float)):
        return ts if ts > 1e12 else ts * 1000
    try:
        return float(ts or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_leaderboard_entry(raw: dict) -> dict:
    return {
        "address": str(raw.get("proxyWallet") or raw.get("address") or "").lower(),
        "rank": int(raw.get("rank") or 0),
        "pnl": float(raw.get("pnl") or 0),
        "volume": float(raw.get("vol") or raw.get("volume") or 0),
        "userName": raw.get("userName"),
        "xUsername": raw.get("xUsername"),
        "verifiedBadge": bool(raw.get("verifiedBadge")),
    }


def normalize_activity(raw: dict) -> dict:
    return {
        "type": str(raw.get("type") or ""),
        "side": str(raw.get("side") or ""),
        "size": float(raw.get("size") or 0),
        "price": float(raw.get("price") or 0),
        "usdcSize": float(raw.get("usdcSize") or 0),
        "asset": str(raw.get("asset") or ""),
        "conditionId": str(raw.get("conditionId") or ""),
        "outcome": str(raw.get("outcome") or ""),
        "timestamp": _ts_ms(raw),
        "transactionHash": str(raw.get("transactionHash") or ""),
        "title": raw.get("title") or "",
        "slug": raw.get("slug") or "",
    }


def normalize_closed_position(raw: dict) -> dict:
    return {
        "asset": str(raw.get("asset") or ""),
        "conditionId": str(raw.get("conditionId") or ""),
        "avgPrice": float(raw.get("avgPrice") or 0),
        "totalBought": float(raw.get("totalBought") or 0),
        "realizedPnl": float(raw.get("realizedPnl") or 0),
        "curPrice": float(raw.get("curPrice") or 0),
        "timestamp": _ts_ms(raw),
        "title": raw.get("title") or "",
        "outcome": raw.get("outcome") or "",
    }


# ======================================================================
# 公开 API
# ======================================================================

def fetch_leaderboard(
    period: str = "WEEK",
    limit: int = 50,
    order_by: str = "PNL",
    category: str = "OVERALL",
) -> list[dict]:
    """排行榜。period: DAY/WEEK/MONTH/ALL；order_by: PNL/VOL；limit ≤ 50。"""
    params = urllib.parse.urlencode({
        "timePeriod": period,
        "orderBy": order_by,
        "category": category,
        "limit": str(min(limit, 50)),
        "offset": "0",
    })
    data = _request_json(f"{DATA_API_BASE}/v1/leaderboard?{params}")
    if not isinstance(data, list):
        return []
    return [normalize_leaderboard_entry(e) for e in data]


def fetch_activity(address: str, limit: int = 100, start_ts: int = None) -> list[dict]:
    """某地址最近活动。start_ts: Unix 秒，只取此后的。"""
    params = {"user": address, "limit": str(min(limit, 500))}
    if start_ts:
        params["start"] = str(start_ts)
    data = _request_json(f"{DATA_API_BASE}/activity?{urllib.parse.urlencode(params)}")
    if not isinstance(data, list):
        return []
    return [normalize_activity(a) for a in data]


def fetch_closed_positions(address: str, limit: int = 50) -> list[dict]:
    """已平仓持仓（算胜率/利润因子用）。"""
    params = urllib.parse.urlencode({
        "user": address,
        "limit": str(min(limit, 50)),
        "sortBy": "TIMESTAMP",
        "sortDirection": "DESC",
    })
    data = _request_json(f"{DATA_API_BASE}/closed-positions?{params}")
    if not isinstance(data, list):
        return []
    return [normalize_closed_position(p) for p in data]


def fetch_account_value(address: str) -> float:
    data = _request_json(f"{DATA_API_BASE}/value?{urllib.parse.urlencode({'user': address})}")
    if isinstance(data, list) and data:
        return float(data[0].get("value") or 0)
    return 0.0


# ======================================================================
# 衍生统计
# ======================================================================

def wallet_stats(address: str, closed_limit: int = 50) -> dict:
    """胜率 / 利润因子 / 已平仓笔数。数据不足时 win_rate 为 None。"""
    closed = fetch_closed_positions(address, limit=closed_limit)
    if len(closed) < 5:
        return {"closed_count": len(closed), "win_rate": None, "profit_factor": None}
    wins = [p for p in closed if p["realizedPnl"] > 0]
    total_win = sum(p["realizedPnl"] for p in wins)
    total_loss = abs(sum(p["realizedPnl"] for p in closed if p["realizedPnl"] < 0))
    return {
        "closed_count": len(closed),
        "win_rate": len(wins) / len(closed),
        "profit_factor": (total_win / total_loss) if total_loss > 0 else (total_win if total_win > 0 else 0.0),
    }
