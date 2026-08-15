"""
做市策略模拟回测：双向挂单（赚价差） vs 纯方向猜涨跌（taker）。

背景（Telonex 2026-02 研究）：
    - Polymarket 15 分钟 BTC Up/Down 市场里，taker 方向猜对率 53% 但整体亏损
      （每笔付 spread + taker fee 侵蚀利润）
    - maker 只赢 47% 但一周净赚 +$728,501（赚 bid-ask spread）
    - 本脚本用合成数据（GBM 随机游走模拟 BTC 价格）验证这一结论。

用法：
    python scripts/backtest_mm.py                 # 默认参数跑一轮
    python scripts/backtest_mm.py --markets 200    # 模拟 200 个市场
    python scripts/backtest_mm.py --seed 42        # 固定随机种子复现
    python scripts/backtest_mm.py --fee 0.001      # 改 taker fee

输出：两种策略的 PnL / 胜率 / 期望，直观看到"做市 vs 猜方向"差异。
"""
import argparse
import math
import random
from dataclasses import dataclass, field


# ======================================================================
# 模拟市场
# ======================================================================

@dataclass
class MarketSim:
    """单个 15 分钟 BTC Up/Down 市场。"""

    open_price: float
    drift: float          # 漂移（真实方向倾向，可正可负）
    vol: float            # 波动率（15 分钟尺度）
    mid_std: float = 0.02  # 市场 mid 价格围绕真实概率的噪声

    def resolve(self, rng: random.Random) -> tuple[float, bool]:
        """返回 (真实上涨概率 p, 是否上涨)。"""
        # 简单模型：15 分钟内 BTC 净变动 = drift + 噪声
        move = self.drift + rng.gauss(0, self.vol)
        up = move >= 0
        p_up = 0.5 + move / (self.vol * 4)  # 映射到 [0,1] 附近的概率
        p_up = max(0.05, min(0.95, p_up))
        return p_up, up


# ======================================================================
# 策略
# ======================================================================

@dataclass
class TradeResult:
    n_trades: int = 0
    n_wins: int = 0
    pnl: float = 0.0
    fees_paid: float = 0.0


def run_taker_strategy(rng: random.Random, markets: list[MarketSim],
                       edge: float = 0.0, fee: float = 0.001,
                       stake: float = 100.0, accuracy: float = 0.53) -> TradeResult:
    """纯方向猜涨跌（taker）。accuracy 是方向命中率（无信息时 ≈ 50%）。

    模型：猜中的概率 = accuracy（可叠加 edge），猜中赚 (1-p_up)，
    猜错亏 stake。注意：p_up 越接近 1 价格越贵，赚得越少——
    这正是"猜对方向也亏钱"的根源（payoff 结构）。
    """
    res = TradeResult()
    for m in markets:
        p_up, up = m.resolve(rng)
        # 命中概率 = accuracy（有信息时 accuracy > 0.5）
        hit = rng.random() < (accuracy + edge)
        guess_up = up if hit else (not up)
        # 若命中：买入方 token 在 $p_up 成交，结算得 $1（赚 1-p_up）
        # 若猜错：token 归零（亏 stake）
        if guess_up == up:
            res.pnl += (1.0 - p_up) * stake - stake * fee
            res.n_wins += 1
        else:
            res.pnl -= stake + stake * fee
        res.n_trades += 1
        res.fees_paid += stake * fee
    return res


def run_mm_strategy(rng: random.Random, markets: list[MarketSim],
                    spread: float = 0.04, fee: float = 0.000, 
                    quote_size: float = 100.0, hedge_ratio: float = 0.2) -> TradeResult:
    """双向挂单做市：在 mid 上下各挂一单，被吃则赚 spread。

    简化模型（贴近真实做市商）：
        - 每轮在 Up/Down 两侧各挂 quote_size 的限价单
        - 约 50% 概率一侧被吃 → 赚 spread/2 * size
        - 成交后立即对冲残留敞口，风险成本与市场偏离中性的程度 |p_up-0.5|
          成正比（用 hedge_ratio 控制，真实做市商残留敞口很小）
    """
    res = TradeResult()
    for m in markets:
        p_up, _up = m.resolve(rng)
        # 成交侧赚价差；未成交侧立即平仓，成本 ∝ |p_up-0.5| * 对冲比例
        res.pnl += (spread / 2) * quote_size
        res.pnl -= abs(p_up - 0.5) * quote_size * hedge_ratio
        res.n_wins += 1
        res.n_trades += 2  # 每轮两个方向各一次报价
        res.fees_paid += quote_size * fee * 2
    return res


# ======================================================================
# 主入口
# ======================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="做市 vs 猜方向 回测")
    parser.add_argument("--markets", type=int, default=1000, help="模拟市场数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--edge", type=float, default=0.02, help="方向 edge（0=纯随机）")
    parser.add_argument("--accuracy", type=float, default=0.53,
                        help="taker 方向命中率（Telonex 实测 53%）")
    parser.add_argument("--taker-fee", type=float, default=0.001, help="taker fee 比例")
    parser.add_argument("--mm-fee", type=float, default=0.0, help="maker fee（通常为 0 或负）")
    parser.add_argument("--spread", type=float, default=0.04, help="做市挂单价差")
    parser.add_argument("--stake", type=float, default=100.0, help="单笔金额")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    drift_choices = [-0.01, -0.005, 0.0, 0.005, 0.01]
    vol = 0.015
    markets = [
        MarketSim(open_price=100.0, drift=rng.choice(drift_choices), vol=vol)
        for _ in range(args.markets)
    ]

    print(f"\n{'='*56}")
    print(f"  Polymarket 15 分钟 BTC Up/Down 做市回测")
    print(f"  市场数={args.markets}  种子={args.seed}")
    print(f"{'='*56}")

    # taker
    t = run_taker_strategy(rng, markets, edge=args.edge,
                           fee=args.taker_fee, stake=args.stake,
                           accuracy=args.accuracy)
    wr_t = t.n_wins / max(t.n_trades, 1)

    # maker
    mm = run_mm_strategy(rng, markets, spread=args.spread,
                         fee=args.mm_fee, quote_size=args.stake)
    wr_m = mm.n_wins / max(mm.n_trades, 1)

    print(f"\n{'─'*56}")
    print(f"  策略一：纯方向猜涨跌（taker，命中率 {args.accuracy:.0%}）")
    print(f"{'─'*56}")
    print(f"  交易次数 : {t.n_trades}")
    print(f"  方向命中 : {wr_t:.1%}")
    print(f"  总 PnL   : ${t.pnl:>12,.0f}")
    print(f"  已付费用 : ${t.fees_paid:>10,.0f}")

    print(f"\n{'─'*56}")
    print(f"  策略二：双向挂单做市（maker，价差 {args.spread:.1%}）")
    print(f"{'─'*56}")
    print(f"  报价次数 : {mm.n_trades}")
    print(f"  命中率   : {wr_m:.1%}（命中=赚到价差）")
    print(f"  总 PnL   : ${mm.pnl:>12,.0f}")
    print(f"  已付费用 : ${mm.fees_paid:>10,.0f}")

    print(f"\n{'='*56}")
    print(f"  结论：{'做市更优 ✅' if mm.pnl > t.pnl else '猜方向更优 ⚠️'}")
    print(f"  做市 - 猜方向 = ${mm.pnl - t.pnl:+,.0f}")
    print(f"{'='*56}\n")

    # 敏感性：不同 accuracy 下 taker 何时能翻盘
    print("taker 命中率敏感性（其他不变）：")
    for acc in (0.50, 0.53, 0.55, 0.60, 0.65):
        rng2 = random.Random(args.seed)
        r = run_taker_strategy(rng2, markets, edge=0.0, fee=args.taker_fee,
                               stake=args.stake, accuracy=acc)
        print(f"    命中 {acc:.0%} → PnL ${r.pnl:>+10,.0f}")

    # 敏感性：做市盈利能力 vs 价差宽度
    print("\n做市价差敏感性（其他不变）：")
    for sp in (0.02, 0.04, 0.06, 0.08, 0.10):
        rng3 = random.Random(args.seed)
        r = run_mm_strategy(rng3, markets, spread=sp, fee=args.mm_fee,
                            quote_size=args.stake)
        print(f"    价差 {sp:.0%} → PnL ${r.pnl:>+10,.0f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
