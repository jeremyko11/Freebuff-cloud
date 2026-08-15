"""
同步版 Token Bucket 限流器（7 类端点官方限流 + 429 指数退避）。

迁移自主 bot d:/A/PPT/rate_limiter.py（借鉴 polymarket-mcp-server）。

用法：
    from src.ratelimit import acquire, handle_429, EndpointCategory
    acquire(EndpointCategory.DATA_API)
    resp = session.get(url)
    if resp.status_code == 429:
        handle_429(EndpointCategory.DATA_API)
"""
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class EndpointCategory(Enum):
    """Polymarket API 端点类别（官方限流配置）。"""
    CLOB_GENERAL = "clob_general"            # 5000/10s
    MARKET_DATA = "market_data"              # 200/10s (/book, /price)
    BATCH_OPS = "batch_ops"                  # 80/10s
    TRADING_BURST = "trading_burst"          # 2400/10s
    TRADING_SUSTAINED = "trading_sustained"  # 24000/10min
    GAMMA_API = "gamma_api"                  # 750/10s
    DATA_API = "data_api"                    # 200/10s


@dataclass
class RateLimitConfig:
    max_tokens: int
    refill_rate: float
    window_seconds: float


RATE_LIMITS: Dict[EndpointCategory, RateLimitConfig] = {
    EndpointCategory.CLOB_GENERAL: RateLimitConfig(5000, 500.0, 10.0),
    EndpointCategory.MARKET_DATA: RateLimitConfig(200, 20.0, 10.0),
    EndpointCategory.BATCH_OPS: RateLimitConfig(80, 8.0, 10.0),
    EndpointCategory.TRADING_BURST: RateLimitConfig(2400, 240.0, 10.0),
    EndpointCategory.TRADING_SUSTAINED: RateLimitConfig(24000, 40.0, 600.0),
    EndpointCategory.GAMMA_API: RateLimitConfig(750, 75.0, 10.0),
    EndpointCategory.DATA_API: RateLimitConfig(200, 20.0, 10.0),
}


class TokenBucket:
    """同步 Token Bucket（线程安全）。token 不足时 acquire() 阻塞等待。"""

    def __init__(self, config: RateLimitConfig):
        self.max_tokens = config.max_tokens
        self.refill_rate = config.refill_rate
        self.tokens = float(config.max_tokens)
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def acquire(self, tokens: int = 1) -> float:
        with self._lock:
            wait_time = 0.0
            while True:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return wait_time
                tokens_needed = tokens - self.tokens
                sleep_time = max(tokens_needed / self.refill_rate, 0.01)
                # 在锁内 sleep 是故意的：限流就是要排队（sleep 释放 GIL）
                time.sleep(sleep_time)
                wait_time += sleep_time

    def try_acquire(self, tokens: int = 1) -> bool:
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def available_tokens(self) -> int:
        with self._lock:
            self._refill()
            return int(self.tokens)


class RateLimiter:
    """多桶限流器 + 429 指数退避（1s → 2s → ... → max 60s）。"""

    def __init__(self):
        self.buckets: Dict[EndpointCategory, TokenBucket] = {
            cat: TokenBucket(cfg) for cat, cfg in RATE_LIMITS.items()
        }
        self._429_backoff: Dict[EndpointCategory, float] = defaultdict(float)
        self._backoff_lock = threading.Lock()

    def acquire(self, category: EndpointCategory, tokens: int = 1) -> float:
        bucket = self.buckets.get(category)
        if not bucket:
            logger.warning("未知端点类别: %s，跳过限流", category)
            return 0.0
        total_wait = 0.0
        with self._backoff_lock:
            backoff_until = self._429_backoff.get(category, 0.0)
            now = time.monotonic()
            if backoff_until > now:
                wait = backoff_until - now
                logger.warning("429 退避中 %s，等待 %.2fs", category.value, wait)
                time.sleep(wait)
                total_wait += wait
        total_wait += bucket.acquire(tokens)
        return total_wait

    def handle_429(self, category: EndpointCategory, retry_after: Optional[int] = None) -> None:
        with self._backoff_lock:
            now = time.monotonic()
            current = self._429_backoff.get(category, 0.0)
            if retry_after:
                backoff_time = float(retry_after)
            elif current > now:
                backoff_time = min((current - now) * 2, 60.0)
            else:
                backoff_time = 1.0
            self._429_backoff[category] = now + backoff_time
            logger.warning("429 错误 %s，退避 %.2fs", category.value, backoff_time)

    def get_status(self) -> Dict[str, Dict]:
        now = time.monotonic()
        out = {}
        for category, bucket in self.buckets.items():
            remaining = max(0.0, self._429_backoff.get(category, 0.0) - now)
            out[category.value] = {
                "available_tokens": bucket.available_tokens(),
                "max_tokens": bucket.max_tokens,
                "backoff_remaining_sec": round(remaining, 2),
                "is_throttled": remaining > 0,
            }
        return out


_rate_limiter: Optional[RateLimiter] = None
_singleton_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        with _singleton_lock:
            if _rate_limiter is None:
                _rate_limiter = RateLimiter()
    return _rate_limiter


def acquire(category: EndpointCategory, tokens: int = 1) -> float:
    return get_rate_limiter().acquire(category, tokens)


def handle_429(category: EndpointCategory, retry_after: Optional[int] = None) -> None:
    get_rate_limiter().handle_429(category, retry_after)


def get_status() -> Dict[str, Dict]:
    return get_rate_limiter().get_status()


def categorize_url(url: str) -> EndpointCategory:
    url_lower = url.lower()
    if "gamma-api" in url_lower:
        return EndpointCategory.GAMMA_API
    if "data-api" in url_lower:
        return EndpointCategory.DATA_API
    if "clob" in url_lower:
        if any(x in url_lower for x in ("/book", "/price", "/midpoint", "/spread")):
            return EndpointCategory.MARKET_DATA
        if any(x in url_lower for x in ("/order", "/trades", "/post", "/cancel", "/market-order")):
            return EndpointCategory.TRADING_BURST
        if "batch" in url_lower:
            return EndpointCategory.BATCH_OPS
    return EndpointCategory.CLOB_GENERAL
