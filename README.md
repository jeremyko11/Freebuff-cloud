# Freebuff-cloud

Polymarket 聪明钱跟踪 bot：自动发现高胜率钱包，监控其开仓/加仓/平仓/拆单建仓，实时 Telegram 推送。**纯跟踪+通知，不下单**（跟单由主交易系统负责）。

## 工作流程

```
排行榜播种（日/周/月各 top N）
        ↓
准入过滤（胜率 ≥55% ∧ 已平仓 ≥10 笔 ∧ PnL ≥$500）
        ↓
做市商剔除（1h 内高频双向成交）
        ↓
0-100 评分（利润40 + 资金效率30 + 胜率30）→ 名单 top 50
        ↓
每 5 分钟轮询名单钱包 activity
        ↓
信号检测（去重）→ Telegram 推送 + SQLite 入库
        ↓
验证闭环（1h/24h 后价格回填，评估信号质量）
```

## 信号类型

| 类型 | 含义 | 触发条件 |
|---|---|---|
| 🟢 OPEN | 新开仓 | 该钱包首次买入某市场，单笔 ≥ $200 |
| 🟡 ADD | 加仓 | 已有持仓的市场再买入，单笔 ≥ $200 |
| 🔴 REDUCE | 减仓/平仓 | 卖出 ≥ $200 |
| 💸 SWEEP | 拆单建仓 | 1h 内 ≥3 笔小额同向累积 ≥ $1000（单笔低于 $200） |

## 快速开始

```bash
git clone https://github.com/jeremyko11/Freebuff-cloud.git
cd Freebuff-cloud
pip install -r requirements.txt
cp .env.example .env        # 填入 TG_BOT_TOKEN / TG_CHAT_ID

python -m src.main seed     # 构建一次名单并预览（不发通知）
python -m src.main          # 启动监控守护
python -m src.main status   # 查看名单/信号/限流状态
```

## 测试

```bash
python -m pytest tests/ -q              # 单元测试（离线）
PYTHONPATH=. python tests/smoke_live.py # 真实 API 冒烟
```

## VPS 部署

见 [deploy/vps.md](deploy/vps.md)（systemd 常驻单元）。

## 配置

全部参数见 [.env.example](.env.example)：名单规模与准入门槛、轮询周期、信号阈值、拆单检测参数均可调。

## 限流

所有请求经过 Token Bucket 限流器（7 类端点官方限额，如 DATA_API 200/10s），429 自动指数退避 1s→60s。迁移自主交易系统同款实现。

## 架构参考

聪明钱定义（排行榜播种+评分+准入+做市商剔除+拆单检测+验证闭环）借鉴 [kydlikebtc/polymarket-whalewatch](https://github.com/kydlikebtc/polymarket-whalewatch) 的设计，Python 实现。

## License

[MIT](LICENSE) © 2026 jeremyko11
