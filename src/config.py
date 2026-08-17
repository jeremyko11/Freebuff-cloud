"""集中配置：环境变量 + 默认值。所有可调参数都在这里。"""
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class TelegramConfig:
    token: str = os.environ.get("TG_BOT_TOKEN", "")
    chat_id: str = os.environ.get("TG_CHAT_ID", "")
    enabled: bool = True

    def __post_init__(self):
        self.enabled = bool(self.token and self.chat_id)


@dataclass
class SmartMoneyConfig:
    # 播种：三个周期排行榜 + 分类榜，取并集
    seed_periods: list = field(default_factory=lambda: ["DAY", "WEEK", "MONTH"])
    seed_per_period: int = _env_int("SM_SEED_PER_PERIOD", 50)
    seed_categories: list = field(default_factory=lambda: _env_list("SM_SEED_CATEGORIES", "OVERALL"))
    # 准入门槛
    min_win_rate: float = _env_float("SM_MIN_WIN_RATE", 0.55)      # 胜率 ≥ 55%
    min_closed_trades: int = _env_int("SM_MIN_CLOSED_TRADES", 10)  # 已平仓 ≥ 10 笔
    min_pnl: float = _env_float("SM_MIN_PNL", 500.0)               # 周期 PnL ≥ $500
    max_wallets: int = _env_int("SM_MAX_WALLETS", 150)              # 名单上限（控 API 用量）
    # 手动追加的观察地址（不经过准入）
    extra_addresses: list = field(default_factory=lambda: _env_list("SM_EXTRA_ADDRESSES"))
    watchlist_path: str = os.environ.get("SM_WATCHLIST_PATH", "data/watchlist.json")
    # 小资金聪明钱发现（聚焦热门市场参与者）
    cap_hot_markets: int = _env_int("CAP_HOT_MARKETS", 5)
    cap_sample_wallets: int = _env_int("CAP_SAMPLE_WALLETS", 40)
    cap_volume_min: float = _env_float("CAP_VOLUME_MIN", 1000.0)
    cap_volume_max: float = _env_float("CAP_VOLUME_MAX", 50000.0)
    # 做市商剔除：1h 内双向成交都活跃即视为 MM
    mm_window_sec: int = _env_int("SM_MM_WINDOW_SEC", 3600)
    mm_min_trades: int = _env_int("SM_MM_MIN_TRADES", 20)
    refresh_hours: int = _env_int("SM_REFRESH_HOURS", 24)          # 名单刷新周期


@dataclass
class MonitorConfig:
    poll_interval_sec: int = _env_int("MON_POLL_INTERVAL_SEC", 2)  # 全名单轮询周期
    activity_limit: int = _env_int("MON_ACTIVITY_LIMIT", 100)
    # 信号金额门槛：低于此值的单笔成交不报
    min_signal_usdc: float = _env_float("MON_MIN_SIGNAL_USDC", 200.0)
    # 拆单建仓：窗口内 ≥ N 笔同向同市场小额，累积 ≥ 门槛
    sweep_window_sec: int = _env_int("MON_SWEEP_WINDOW_SEC", 3600)
    sweep_min_trades: int = _env_int("MON_SWEEP_MIN_TRADES", 3)
    sweep_min_total_usdc: float = _env_float("MON_SWEEP_MIN_TOTAL_USDC", 1000.0)
    # 信号去重窗口
    dedup_window_sec: int = _env_int("MON_DEDUP_WINDOW_SEC", 3600)
    # 验证闭环：信号后回填 1h / 24h 走势
    verify_enabled: bool = os.environ.get("MON_VERIFY", "1") not in ("0", "false")


@dataclass
class XConfig:
    """X (Twitter) API：搜索社区推荐的 Polymarket 聪明钱。"""
    bearer: str = os.environ.get("X_BEARER", "")
    search_query: str = os.environ.get("X_SEARCH_QUERY",
        "polymarket smart money whale -is:retweet")
    max_results: int = _env_int("X_MAX_RESULTS", 20)


@dataclass
class Config:
    db_path: Path = Path(os.environ.get("FB_DB_PATH", "data/freebuff.db"))
    log_level: str = os.environ.get("FB_LOG_LEVEL", "INFO")
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    smart: SmartMoneyConfig = field(default_factory=SmartMoneyConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    x: XConfig = field(default_factory=XConfig)

    def __post_init__(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
