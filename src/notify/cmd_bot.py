"""Telegram 命令监听器：/filter 内联按钮筛选管理。

长轮询 getUpdates，处理 /filter 命令与内联键盘回调，更新 user_filters。
与推送 bot 共用 token；用 offset 增量拉取 update。
"""
import json
import logging
import threading
import time

import requests

from src.config import Config
from src.store.db import Store
from src.notify.telegram import TG_API, _get_session
from src.smart.filter import DEFAULT_FILTER

logger = logging.getLogger(__name__)

MARKETS = ["足球", "网球", "棒球", "电竞", "橄榄球", "篮球", "搏击", "加密", "政治"]
SOURCES = ["排行榜", "社区", "小资金", "手动"]
_stop = threading.Event()


def _send(cfg, method, **kwargs):
    url = f"{TG_API}/bot{cfg.telegram.token}/{method}"
    try:
        return _get_session().post(url, json=kwargs, timeout=10).json()
    except Exception as e:
        logger.warning("TG %s 失败: %s", method, e)
        return {}


def _load_flt(store) -> dict:
    base = dict(DEFAULT_FILTER)
    cur = store.get_filter("push_filter", None)
    if cur and isinstance(cur, dict):
        merged = dict(base)
        merged.update(cur)
        # 确保 enabled_sources 完整
        es = dict(base["enabled_sources"])
        if isinstance(cur.get("enabled_sources"), dict):
            es.update(cur["enabled_sources"])
        merged["enabled_sources"] = es
        return merged
    return base


def _save_flt(store, flt):
    store.set_filter("push_filter", flt)


def _market_keyboard(flt):
    """市场分类开关按钮（toggle blocked）。"""
    rows = []
    for m in MARKETS:
        status = "🔴" if m in flt["blocked_markets"] else "🟢"
        rows.append([{"text": f"{status} {m}", "callback_data": f"mk:{m}"}])
    return rows


def _src_keyboard(flt):
    rows = []
    for s in SOURCES:
        on = flt["enabled_sources"].get(s, 1)
        status = "🟢" if on else "⚪"
        rows.append([{"text": f"{status} {s}", "callback_data": f"src:{s}"}])
    return rows


def _main_keyboard():
    return [
        [{"text": "🎫 市场分类", "callback_data": "menu:markets"}],
        [{"text": "📡 来源开关", "callback_data": "menu:sources"}],
        [{"text": "🔽 金额门槛", "callback_data": "menu:money"}],
        [{"text": "🔁 重置默认", "callback_data": "menu:reset"}],
    ]


def _money_keyboard(flt):
    opts = [100, 200, 500, 1000, 2000, 5000]
    rows = []
    for m in opts:
        mark = "✓" if flt.get("min_usdc", 200) == m else ""
        rows.append([{"text": f"${m} {mark}", "callback_data": f"money:{m}"}])
    return rows


def _show_filter(cfg, chat_id, flt):
    text = (
        "🎛️ 推送过滤设置\n\n"
        f"屏蔽市场: {', '.join(flt['blocked_markets']) or '无'}\n"
        f"只收市场: {', '.join(flt['allowed_markets']) or '全部'}\n"
        f"金额门槛: ${flt.get('min_usdc',200)}\n"
        f"来源开关: {', '.join(k for k,v in flt['enabled_sources'].items() if not v) or '全开'}\n"
        "（🟢=接收 🔴=屏蔽）"
    )
    _send(cfg, "sendMessage", chat_id=chat_id, text=text, reply_markup={"inline_keyboard": _main_keyboard()})


def _handle_cmd(cfg, store, msg):
    chat = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat or not text.startswith("/filter"):
        return
    flt = _load_flt(store)
    parts = text.split()
    if len(parts) == 1:
        _show_filter(cfg, chat, flt)
        return
    # /filter block 电竞,加密  或  /filter only 足球  或 /filter min 500 或 /filter reset
    cmd = parts[1].lower()
    args = "，".join(parts[2:]).replace("，", ",")
    if cmd == "block":
        for m in args.split(","):
            m = m.strip()
            if m and m not in flt["blocked_markets"]:
                flt["blocked_markets"].append(m)
        _save_flt(store, flt)
        _send(cfg, "sendMessage", chat_id=chat, text=f"✅ 已屏蔽: {args or '—'}")
    elif cmd == "unblock":
        for m in args.split(","):
            m = m.strip()
            if m in flt["blocked_markets"]:
                flt["blocked_markets"].remove(m)
        _save_flt(store, flt)
        _send(cfg, "sendMessage", chat_id=chat, text=f"✅ 已解除屏蔽: {args or '—'}")
    elif cmd == "only":
        flt["allowed_markets"] = [m.strip() for m in args.split(",") if m.strip()]
        flt["blocked_markets"] = []
        _save_flt(store, flt)
        _send(cfg, "sendMessage", chat_id=chat, text=f"✅ 只收市场: {args or '全部'}")
    elif cmd == "min":
        try:
            m = int(args)
            flt["min_usdc"] = m
            _save_flt(store, flt)
            _send(cfg, "sendMessage", chat_id=chat, text=f"✅ 金额门槛: ${m}")
        except Exception:
            _send(cfg, "sendMessage", chat_id=chat, text="用法: /filter min 500")
    elif cmd == "reset":
        _save_flt(store, DEFAULT_FILTER)
        _send(cfg, "sendMessage", chat_id=chat, text="✅ 已重置默认（全收）")
    else:
        _send(cfg, "sendMessage", chat_id=chat, text="未知筛选命令，用 /filter 打开菜单")


def _handle_callback(cfg, store, cb):
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat = msg.get("chat", {}).get("id")
    cb_id = cb.get("id")
    flt = _load_flt(store)
    if data.startswith("menu:"):
        menu = data.split(":")[1]
        if menu == "markets":
            kb = _market_keyboard(flt)
            _send(cfg, "editMessageText", chat_id=chat, message_id=msg.get("message_id"),
                  text="🎫 点选市场开关（屏蔽/接收）", reply_markup={"inline_keyboard": kb})
        elif menu == "sources":
            kb = _src_keyboard(flt)
            _send(cfg, "editMessageText", chat_id=chat, message_id=msg.get("message_id"),
                  text="📡 来源开关", reply_markup={"inline_keyboard": kb})
        elif menu == "money":
            kb = _money_keyboard(flt)
            _send(cfg, "editMessageText", chat_id=chat, message_id=msg.get("message_id"),
                  text=f"💰 金额门槛（当前 ${flt.get('min_usdc',200)}）", reply_markup={"inline_keyboard": kb})
        elif menu == "reset":
            _save_flt(store, DEFAULT_FILTER)
            _send(cfg, "answerCallbackQuery", callback_query_id=cb_id, text="已重置默认")
    elif data.startswith("mk:"):
        m = data.split(":", 1)[1]
        if m in flt["blocked_markets"]:
            flt["blocked_markets"].remove(m)
        else:
            flt["blocked_markets"].append(m)
        _save_flt(store, flt)
        _send(cfg, "editMessageReplyMarkup", chat_id=chat, message_id=msg.get("message_id"),
              reply_markup={"inline_keyboard": _market_keyboard(flt)})
        _send(cfg, "answerCallbackQuery", callback_query_id=cb_id, text=f"{m}: {'屏蔽' if m in flt['blocked_markets'] else '接收'}")
    elif data.startswith("src:"):
        s = data.split(":", 1)[1]
        flt["enabled_sources"][s] = 0 if flt["enabled_sources"].get(s, 1) else 1
        _save_flt(store, flt)
        _send(cfg, "editMessageReplyMarkup", chat_id=chat, message_id=msg.get("message_id"),
              reply_markup={"inline_keyboard": _src_keyboard(flt)})
        _send(cfg, "answerCallbackQuery", callback_query_id=cb_id, text=f"{s}: {'开' if flt['enabled_sources'].get(s) else '关'}")
    elif data.startswith("money:"):
        m = int(data.split(":")[1])
        flt["min_usdc"] = m
        _save_flt(store, flt)
        _send(cfg, "editMessageReplyMarkup", chat_id=chat, message_id=msg.get("message_id"),
              reply_markup={"inline_keyboard": _money_keyboard(flt)})
        _send(cfg, "answerCallbackQuery", callback_query_id=cb_id, text=f"门槛 ${m}")


def run_cmd_bot(cfg: Config, store: Store = None) -> int:
    store = store or Store(cfg.db_path)
    last_update = 0
    while not _stop.is_set():
        try:
            updates = _get_session().post(
                f"{TG_API}/bot{cfg.telegram.token}/getUpdates",
                json={"offset": last_update + 1, "timeout": 10}, timeout=15).json()
            for u in updates.get("result", []):
                last_update = max(last_update, u.get("update_id", 0))
                msg = u.get("message")
                cb = u.get("callback_query")
                if msg:
                    _handle_cmd(cfg, store, msg)
                elif cb:
                    _handle_callback(cfg, store, cb)
        except Exception as e:
            logger.warning("cmd bot 轮询异常: %s", e)
            time.sleep(2)
        time.sleep(0.5)
    return 0


def _sig(signum, frame):
    _stop.set()
