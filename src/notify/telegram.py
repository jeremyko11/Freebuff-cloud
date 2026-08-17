"""Telegram 推送（token/chat_id 全部配置化，绝不硬编码）。"""
import logging
import time
from typing import Optional

import requests

from src.config import TelegramConfig
from src.monitor.signals import Signal
from src.smart.tagging import with_emoji

logger = logging.getLogger(__name__)

TG_API = "https://api.telegram.org"

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def send_message(cfg: TelegramConfig, html: str, max_attempts: int = 3) -> bool:
    """发送 HTML 消息。失败重试，仍失败返回 False（不抛异常，通知不能拖垮主循环）。"""
    if not cfg.enabled:
        return False
    for attempt in range(max_attempts):
        try:
            resp = _get_session().post(
                f"{TG_API}/bot{cfg.token}/sendMessage",
                json={
                    "chat_id": cfg.chat_id,
                    "text": html,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if resp.status_code == 429:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", 3))
                logger.warning("TG 限流，等待 %ds", retry_after)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("TG 发送失败（第%d次）: %s", attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    return False


_TYPE_EMOJI = {
    "OPEN": "\U0001F7E2",   # 🟢 新开仓
    "ADD": "\U0001F7E1",    # 🟡 加仓
    "REDUCE": "\U0001F534", # 🔴 减仓/平仓
    "SWEEP": "\U0001F4B8",  # 💸 拆单建仓
}


def format_signal(s: Signal) -> str:
    """信号 → Telegram HTML。"""
    emoji = _TYPE_EMOJI.get(s.type, "•")
    type_label = {"OPEN": "新开仓", "ADD": "加仓", "REDUCE": "减仓/平仓", "SWEEP": "拆单建仓"}.get(s.type, s.type)
    who = s.wallet_name or f"{s.address[:8]}…{s.address[-4:]}"
    if getattr(s, "tags", None):
        who = f"{who} 「{'·'.join(with_emoji(t) for t in s.tags)}」"
    title = s.title or (s.conditionId[:16] if s.conditionId else "（未知市场）")
    url = f"https://polymarket.com/market/{s.slug}" if (s.slug and s.slug.strip()) else ""
    lines = [
        f"{emoji} <b>[{type_label}]</b> {who}",
    ]
    if getattr(s, "market_label", ""):
        lines.append(f"🏷️ {s.market_label}")
    wallet_url = f"https://polymarket.com/profile/{s.address}"
    lines.append(f"👤 <a href='{wallet_url}'>钱包主页</a>")
    lines += [
        f"市场：{title}",
        f"方向：<b>{s.side} {s.outcome}</b> @ {s.price:.3f}",
        f"金额：<b>${s.usdc:,.0f}</b>",
    ]
    if s.type == "SWEEP":
        lines.append(f"累积：{s.trade_count} 笔小单")
    if url:
        lines.append(url)
    if s.tx_hashes:
        lines.append(f"<a href='https://polygonscan.com/tx/{s.tx_hashes[0]}'>tx</a>")
    return "\n".join(lines)
