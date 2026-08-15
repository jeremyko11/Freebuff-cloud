# VPS 部署指南

目标：Ubuntu 22.04/24.04，以 systemd 服务 7×24 运行。

## 1. 环境准备

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo useradd -r -m -d /home/deploy -s /bin/bash deploy 2>/dev/null || true
sudo mkdir -p /opt/freebuff-cloud
sudo chown deploy:deploy /opt/freebuff-cloud
```

## 2. 拉代码 + 装依赖

```bash
sudo -u deploy git clone https://github.com/jeremyko11/Freebuff-cloud.git /opt/freebuff-cloud
cd /opt/freebuff-cloud
sudo -u deploy python3 -m venv .venv
sudo -u deploy .venv/bin/pip install -r requirements.txt
```

## 3. 配置 .env

```bash
sudo -u deploy cp .env.example .env
sudo -u deploy nano .env
```

必填项：

| 变量 | 说明 |
|------|------|
| `TG_BOT_TOKEN` | @BotFather 创建 bot 后获得 |
| `TG_CHAT_ID` | 先给 bot 发一条消息，再访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 取 `chat.id` |

其余参数（名单门槛、轮询周期、拆单阈值）有默认值，可按需调。

## 4. 验证

```bash
cd /opt/freebuff-cloud
sudo -u deploy PYTHONPATH=. .venv/bin/python tests/smoke_live.py   # 真实 API 冒烟
sudo -u deploy .venv/bin/python -m src.main seed                   # 构建一次聪明钱名单
sudo -u deploy .venv/bin/python -m src.main status                 # 查看名单/信号
```

## 5. 安装 systemd 服务

```bash
sudo cp deploy/freebuff.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now freebuff
```

常用命令：

```bash
sudo systemctl status freebuff        # 运行状态
sudo journalctl -u freebuff -f        # 实时日志
sudo systemctl restart freebuff       # 改 .env 后重启生效
```

## 6. 升级

```bash
cd /opt/freebuff-cloud
sudo -u deploy git pull
sudo -u deploy .venv/bin/pip install -r requirements.txt
sudo systemctl restart freebuff
```

## 7. 数据备份

SQLite 单文件，直接拷走即可（建议 cron 每日一次）：

```bash
# /etc/cron.d/freebuff-backup
0 4 * * * deploy sqlite3 /opt/freebuff-cloud/data/freebuff.db ".backup /opt/freebuff-cloud/data/backup.db"
```
