"""信号把握度（confidence）评分。

核心：以"钱包历史胜率"为把握主依据（这人准不准），叠加金额(下注决心)微调。
价格作为参考信息标注（不参与惩罚——价格0.5处买卖是常态）。

  把握度 = 胜率分(0~100) + 金额加成(0~15)
  等级：>=75 高 🟢 / >=50 中 🟡 / <50 低 🔴
"""
from __future__ import annotations


def confidence(wallet_win_rate: float | None, price: float | None,
               usdc: float | None) -> float:
    # 胜率分（0~100）：0.5→0, 1.0→100
    wr = wallet_win_rate if wallet_win_rate is not None else 0.55
    wr_score = 100.0 * min(max((wr - 0.5) / 0.5, 0.0), 1.0)
    # 金额加成（0~15）：$50k 封顶
    u = (usdc or 0)
    money_bonus = 15.0 * min(u / 50000.0, 1.0)
    conf = min(wr_score + money_bonus, 100.0)
    return round(conf)


def level(conf: float) -> str:
    return "高" if conf >= 75 else ("中" if conf >= 50 else "低")


def level_emoji(conf: float) -> str:
    return "🟢" if conf >= 75 else ("🟡" if conf >= 50 else "🔴")


def should_push(conf: float) -> bool:
    """低把握不推（减少噪音）。门槛 30，高于在排行榜聪明钱中常见的 12-24 分区间。"""
    return conf >= 20


def format_conf(conf: float) -> str:
    return f"{level_emoji(conf)} 把握{level(conf)} {conf}分"
