"""Binance 行情客户端：BTC 实时价格 + 价格突变检测。

原理（Polymarket 5 分钟市场的经典 edge）：
    5 分钟/15 分钟 BTC Up/Down 市场锚定 Chainlink 的 TWAP 结算价，
    而 Binance 现货价格先动、Polymarket 市场价格滞后。
    监听 Binance 价格突变即可提前预判 Polymarket 市场走向。

用法：
    client = BinanceClient(symbol="BTCUSDT")
    price = client.fetch_price()          # 单次获取
    client.window_move(sec=60)            # 最近 60 秒价格变动比例
"""
import logging
import threading
import time
from collections import deque
from typing import Deque, Optional

import requests

logger = logging.getLogger(__name__)

BINANCE_API = "https://api.binance.com"
# 备用节点（主节点被限流/区域不可达时切换）
_BINANCE_MIRRORS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": "freebuff-cloud/0.1"})
    return _session


class BinanceClient:
    def __init__(self, symbol: str = "BTCUSDT", mirror_ttl: float = 600.0):
        self.symbol = symbol.upper()
        self._mirror_idx = 0
        self._mirror_switch_ts = 0.0
        self._mirror_ttl = mirror_ttl
        self._lock = threading.Lock()
        self._history: Deque[tuple[float, float]] = deque(maxlen=600)  # (ts, price)
        self._last_price: Optional[float] = None
        self._last_ts = 0.0

    # ------------------------------------------------------------------
    def fetch_price(self, timeout: float = 5.0) -> Optional[float]:
        """获取当前价格。失败自动切换镜像节点。"""
        url = f"{self._current_mirror()}/api/v3/ticker/price?symbol={self.symbol}"
        try:
            resp = _get_session().get(url, timeout=timeout)
            resp.raise_for_status()
            price = float(resp.json()["price"])
            now = time.time()
            with self._lock:
                self._history.append((now, price))
                self._last_price = price
                self._last_ts = now
            return price
        except Exception as e:
            self._switch_mirror()
            logger.warning("Binance 价格获取失败 %s: %s", url, e)
            return None

    def _current_mirror(self) -> str:
        now = time.time()
        # 每个镜像用满 ttl 后轮换，避免单一节点长期限流
        if now - self._mirror_switch_ts > self._mirror_ttl:
            self._mirror_idx = (self._mirror_idx + 1) % len(_BINANCE_MIRRORS)
            self._mirror_switch_ts = now
        return _BINANCE_MIRRORS[self._mirror_idx]

    def _switch_mirror(self) -> None:
        self._mirror_idx = (self._mirror_idx + 1) % len(_BINANCE_MIRRORS)
        self._mirror_switch_ts = time.time()

    # ------------------------------------------------------------------
    def last_price(self) -> Optional[float]:
        with self._lock:
            return self._last_price

    def price_age(self) -> float:
        with self._lock:
            return time.time() - self._last_ts if self._last_ts else float("inf")

    def window_move(self, sec: float) -> Optional[float]:
        """窗口内价格变动比例（最新 vs 窗口起点），数据不足返回 None。"""
        with self._lock:
            if not self._history:
                return None
            now = time.time()
            oldest = None
            for ts, price in self._history:
                if now - ts <= sec:
                    oldest = (ts, price)
                    break
            if oldest is None or self._last_price is None:
                return None
            base = oldest[1]
            if base == 0:
                return None
            return (self._last_price - base) / base

    def recent_moves(self, sec: float) -> list[tuple[float, float]]:
        """窗口内的 (相对秒数, 变动比例) 序列，用于绘图/日志。"""
        with self._lock:
            now = time.time()
            base = None
            for ts, price in self._history:
                if now - ts <= sec:
                    base = price
                    break
            if base is None or base == 0:
                return []
            out = []
            for ts, price in self._history:
                if now - ts <= sec:
                    out.append((round(now - ts, 1), round((price - base) / base, 6)))
            return list(reversed(out))
