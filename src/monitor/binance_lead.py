"""
Binance 领先信号检测（5 分钟/15 分钟 BTC 市场专用）。

原理：
    Polymarket 的 BTC Up/Down 短周期市场以 Chainlink TWAP 结算，
    Binance 现货价格通常先动，Polymarket 市场因信息传递滞后而慢半拍。
    监听 Binance 价格突变 → 检查 Polymarket 市场是否跟上 →
    如果明显滞后则产出"领先信号"（提示 Polymarket 即将跟随）。

信号 vs 聪明钱信号的区别：
    聪明钱信号：某个钱包开仓/加仓（社交信号）
    领先信号： 市场价格即将移动（价格信号）——纯提示，不保证方向正确
"""
import logging
import time
from dataclasses import dataclass

from src.api import binance as binance_api
from src.config import BinanceLeadConfig

logger = logging.getLogger(__name__)


@dataclass
class LeadSignal:
    symbol: str          # BTCUSDT
    direction: str       # UP / DOWN
    binance_move: float  # Binance 窗口内变动比例
    binance_price: float
    pm_price: float      # Polymarket 当前 Up token 价格
    pm_lag: float        # Polymarket 滞后幅度（预测值 vs 实际值差距）
    reason: str


# 市场缓存：避免每轮都查 gamma API
_market_cache: dict = {"markets": [], "ts": 0.0}
_MARKET_TTL = 300.0


def _fetch_btc_5m_markets(limit: int = 5) -> list[dict]:
    """从 gamma API 拉取当前活跃的 BTC 5 分钟 Up/Down 市场。失败返回空列表。"""
    now = time.time()
    if _market_cache["markets"] and now - _market_cache["ts"] < _MARKET_TTL:
        return _market_cache["markets"]

    try:
        import requests
        url = ("https://gamma-api.polymarket.com/events?closed=false&limit=100"
               "&slug=bitcoin&order=createdAt")
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        events = resp.json() if isinstance(resp.json(), list) else []
    except Exception as e:
        logger.warning("gamma 市场拉取失败: %s", e)
        return []

    markets = []
    for ev in events:
        slug = (ev.get("slug") or "").lower()
        # 5 分钟 / 15 分钟 BTC 市场命名通常含 "bitcoin" 和 "5" 或 "15 minutes"
        if "bitcoin" not in slug:
            continue
        for m in ev.get("markets", []):
            q = (m.get("question") or "").lower()
            if any(k in q for k in ("5 minute", "15 minute", "up or down", "go up", "go down")):
                markets.append({
                    "question": m.get("question"),
                    "slug": m.get("slug"),
                    "condition_id": m.get("conditionId"),
                    "outcomes": m.get("outcomes"),
                    "token_ids": m.get("clobTokenIds"),
                })
                if len(markets) >= limit:
                    break
        if len(markets) >= limit:
            break

    _market_cache["markets"] = markets
    _market_cache["ts"] = now
    return markets


def _pm_up_price(market: dict) -> float:
    """取市场 Up token 的 mid 价格（0~1）。失败返回 0.5 中性值。"""
    try:
        from src.api.prices import fetch_mid
        token_ids = market.get("token_ids") or []
        if not token_ids:
            return 0.5
        # clobTokenIds 顺序一般与 outcomes 一致：[YES/Up, NO/Down]
        mid = fetch_mid(token_ids[0])
        return mid if mid is not None else 0.5
    except Exception:
        return 0.5


def check_lead(client: binance_api.BinanceClient, cfg: BinanceLeadConfig) -> list[LeadSignal]:
    """检查一次领先信号。返回信号列表（可能为空）。"""
    if not cfg.enabled:
        return []

    move = client.window_move(cfg.window_sec)
    price = client.last_price()
    if move is None or price is None:
        return []
    if abs(move) < cfg.move_threshold:
        return []  # Binance 没明显变动，不检查

    markets = _fetch_btc_5m_markets()
    if not markets:
        return []

    signals: list[LeadSignal] = []
    for m in markets:
        pm_up = _pm_up_price(m)
        # Binance 涨 → Polymarket Up 应该接近 1；跌 → 应接近 0
        expected = 0.5 + move * 5.0  # 粗略映射：1% 变动 → 5 美分价格移动
        expected = max(0.05, min(0.95, expected))
        lag = expected - pm_up if move > 0 else pm_up - expected
        if lag > cfg.lag_threshold:
            signals.append(LeadSignal(
                symbol=cfg.symbol,
                direction="UP" if move > 0 else "DOWN",
                binance_move=move,
                binance_price=price,
                pm_price=pm_up,
                pm_lag=lag,
                reason=f"Binance {move:+.3%} 已动，Polymarket 仍 {pm_up:.0%}，预计滞后 {lag:.0%}",
            ))
    return signals


def format_lead(s: LeadSignal) -> str:
    arrow = "📈" if s.direction == "UP" else "📉"
    return (
        f"{arrow} <b>[Binance 领先信号]</b> {s.symbol}\n"
        f"方向：<b>{s.direction}</b>（Binance {s.binance_move:+.3%}，现价 ${s.binance_price:,.0f}）\n"
        f"Polymarket 当前 Up 价格：{s.pm_price:.0%}，预计滞后 {s.pm_lag:.0%}\n"
        f"说明：{s.reason}"
    )


def run_lead_loop(cfg: BinanceLeadConfig, telegram=None) -> None:
    """独立运行 Binance 领先信号监控（Ctrl+C 退出）。"""
    import signal as _signal
    client = binance_api.BinanceClient(cfg.symbol)
    stop = {"flag": False}

    def _stop_handler(signum, frame):
        stop["flag"] = True

    _signal.signal(_signal.SIGINT, _stop_handler)
    _signal.signal(_signal.SIGTERM, _stop_handler)
    logger.info("Binance 领先信号监控启动：%s 每 %ds 轮询", cfg.symbol, cfg.poll_interval_sec)

    while not stop["flag"]:
        t0 = time.time()
        try:
            sigs = check_lead(client, cfg)
            for s in sigs:
                text = format_lead(s)
                print(text)
                if telegram:
                    telegram.send_message(text)
        except Exception:
            logger.exception("领先信号检查异常")
        sleep_left = max(0, cfg.poll_interval_sec - (time.time() - t0))
        end = time.time() + sleep_left
        while not stop["flag"] and time.time() < end:
            time.sleep(min(2.0, end - time.time()))
    logger.info("领先信号监控已停止")
