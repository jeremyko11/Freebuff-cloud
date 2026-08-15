"""CLOB 价格接口（验证闭环用）：asset token id → mid price。"""
import logging
import urllib.parse
from typing import Optional

from src.api.data_api import _request_json
from src.ratelimit import EndpointCategory, acquire

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"

_mid_cache: dict[str, tuple[float, float]] = {}


def fetch_mid(asset_token_id: str, ttl_sec: float = 60.0) -> Optional[float]:
    """某 outcome token 的中间价。asset 为空或失败返回 None。"""
    import time
    if not asset_token_id:
        return None
    now = time.time()
    hit = _mid_cache.get(asset_token_id)
    if hit and now - hit[1] < ttl_sec:
        return hit[0]
    acquire(EndpointCategory.MARKET_DATA)
    try:
        from src.api.data_api import _get_session
        url = f"{CLOB_BASE}/midpoint?{urllib.parse.urlencode({'token_id': asset_token_id})}"
        resp = _get_session().get(url, timeout=5)
        if resp.status_code == 429:
            from src.ratelimit import handle_429
            handle_429(EndpointCategory.MARKET_DATA)
            return None
        resp.raise_for_status()
        mid = float(resp.json().get("mid"))
        _mid_cache[asset_token_id] = (mid, now)
        return mid
    except Exception as e:
        logger.debug("midpoint 失败 %s: %s", asset_token_id[:16], e)
        return None
