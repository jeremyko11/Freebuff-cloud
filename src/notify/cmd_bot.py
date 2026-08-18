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

MARKETS = ["足球", "网球", "棒球", "电竞", "橄榄球", "篮球", "搏击", "加密", "政治", "娱乐", "经济", "天气", "其他"]
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
    rows = []
    for m in MARKETS:
        status = "🔴" if m in flt["blocked_markets"] else "🟢"
        rows.append([{"text": f"{status} {m}", "callback_data": f"mk:{m}"}])
    rows.append([{"text": "⬅️ 返回主页", "callback_data": "menu:home"}])
    return rows


def _src_keyboard(flt):
    rows = []
    for src in SOURCES:
        on = flt["enabled_sources"].get(src, 1)
        status = "🟢" if on else "⚪"
        rows.append([{"text": f"{status} {src}", "callback_data": f"src:{src}"}])
    rows.append([{"text": "⬅️ 返回主页", "callback_data": "menu:home"}])
    return rows


def _main_keyboard():
    return [
        [{"text": "🎫 市场分类", "callback_data": "menu:markets"}],
        [{"text": "📡 来源开关", "callback_data": "menu:sources"}],
        [{"text": "💰 金额门槛", "callback_data": "menu:money"}],
        [{"text": "🎯 把握度门槛", "callback_data": "menu:conf"}],
        [{"text": "👛 白名单钱包", "callback_data": "menu:wallets"}],
        [{"text": "🔁 重置默认", "callback_data": "menu:reset"}],
    ]


def _money_keyboard(flt):
    opts = [100, 200, 500, 1000, 2000, 5000]
    rows = []
    for m in opts:
        mark = "✓" if flt.get("min_usdc", 200) == m else ""
        rows.append([{"text": f"${m} {mark}", "callback_data": f"money:{m}"}])
    rows.append([{"text": "⬅️ 返回主页", "callback_data": "menu:home"}])
    return rows


def _conf_keyboard(flt):
    opts = [0, 20, 40, 60, 80]
    rows = []
    cur = flt.get("min_conf") or 0
    rows.append([{"text": "💡 0=不限（默认）", "callback_data": "conf:none"}])
    for c in opts:
        if c == 0:
            continue
        label = {20: "≥20 低⚠️", 40: "≥40 中下", 60: "≥60 中", 80: "≥80 高🟢"}.get(c, f"≥{c}")
        mark = "✓" if cur == c else ""
        rows.append([{"text": f"{label} {mark}", "callback_data": f"conf:{c}"}])
    rows.append([{"text": "⬅️ 返回主页", "callback_data": "menu:home"}])
    return rows


def _wallet_keyboard(flt):
    ws = flt.get("allowed_wallets") or []
    rows = [[{"text": f"👛 当前白名单 {len(ws)} 个", "callback_data": "wallet:info"}]]
    if ws:
        rows.append([{"text": "🗑 清空白名单", "callback_data": "wallet:clear"}])
    rows.append([{"text": "⬅️ 返回主页", "callback_data": "menu:home"}])
    return rows


def _filter_summary(flt) -> str:
    ws = flt.get("allowed_wallets") or []
    return (
        "🎛️ 推送过滤\n\n"
        f"屏蔽市场: {', '.join(flt.get('blocked_markets') or []) or '—'}\n"
        f"只收市场: {', '.join(flt.get('allowed_markets') or []) or '全部'}\n"
        f"金额门槛: ${flt.get('min_usdc',200)}\n"
        f"把握度: {'不限' if not (flt.get('min_conf') or 0) else '≥%d分' % flt['min_conf']}\n"
        f"白名单: {len(ws)} 个钱包\n"
        f"来源关闭: {', '.join(k for k,v in (flt.get('enabled_sources') or {}).items() if not v) or '全开'}"
    )


def _show_filter(cfg, chat_id, flt):
    text = _filter_summary(flt) + "\n\n（🟢=接收 🔴=屏蔽）"
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
    elif cmd in ("wallet", "w"):
        addr = args.strip()
        allowed = flt.get("allowed_wallets") or []
        if addr:
            a = addr.lower()
            if a and a not in allowed:
                allowed.append(a)
                _send(cfg, "sendMessage", chat_id=chat, text=f"✅ 已加入白名单: {addr[:14]}...")
            else:
                _send(cfg, "sendMessage", chat_id=chat, text="❌ 地址无效或已存在")
        else:
            _send(cfg, "sendMessage", chat_id=chat,
                  text=f"当前白名单 {len(allowed)} 个: {', '.join(allowed) or '无'}\n用 /filter wallet 0x地址 添加")
        flt["allowed_wallets"] = allowed
        _save_flt(store, flt)
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
        if menu == "home":
            _send(cfg, "editMessageText", chat_id=chat, message_id=msg.get("message_id"),
                  text=_filter_summary(flt), reply_markup={"inline_keyboard": _main_keyboard()})
            _send(cfg, "answerCallbackQuery", callback_query_id=cb_id, text="")
        elif menu == "markets":
            kb = _market_keyboard(flt)
            _send(cfg, "editMessageText", chat_id=chat, message_id=msg.get("message_id"),
                  text="🎫 市场分类（点⚪/🔴切换屏蔽）", reply_markup={"inline_keyboard": kb})
        elif menu == "sources":
            kb = _src_keyboard(flt)
            _send(cfg, "editMessageText", chat_id=chat, message_id=msg.get("message_id"),
                  text="📡 来源开关", reply_markup={"inline_keyboard": kb})
        elif menu == "money":
            kb = _money_keyboard(flt)
            _send(cfg, "editMessageText", chat_id=chat, message_id=msg.get("message_id"),
                  text=f"💰 金额门槛（当前 ${flt.get('min_usdc',200)}）", reply_markup={"inline_keyboard": kb})
        elif menu == "conf":
            kb = _conf_keyboard(flt)
            cur = flt.get("min_conf") or 0
            _send(cfg, "editMessageText", chat_id=chat, message_id=msg.get("message_id"),
                  text=f"🎯 把握度门槛（当前 {'不限' if not cur else '≥%d分' % cur}）\n把握分来自钱包胜率+下注金额", reply_markup={"inline_keyboard": kb})
        elif menu == "wallets":
            kb = _wallet_keyboard(flt)
            ws = flt.get("allowed_wallets") or []
            _send(cfg, "editMessageText", chat_id=chat, message_id=msg.get("message_id"),
                  text="👛 白名单钱包（当前 %d 个）\n使用: /filter wallet 0x地址 添加" % len(ws),
                  reply_markup={"inline_keyboard": kb})
        elif menu == "reset":
            _save_flt(store, DEFAULT_FILTER)
            _send(cfg, "answerCallbackQuery", callback_query_id=cb_id, text="已重置默认（全收）")
            _send(cfg, "editMessageText", chat_id=chat, message_id=msg.get("message_id"),
                  text=_filter_summary(DEFAULT_FILTER), reply_markup={"inline_keyboard": _main_keyboard()})
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
    elif data.startswith("wallet:"):
        act = data.split(":")[1]
        if act == "toggle":
            ws = flt.get("allowed_wallets") or []
            flt["allowed_wallets"] = []
            _send(cfg, "answerCallbackQuery", callback_query_id=cb_id, text="已改回全收")
        elif act == "clear":
            flt["allowed_wallets"] = []
            _send(cfg, "answerCallbackQuery", callback_query_id=cb_id, text="已清空白名单")
        _save_flt(store, flt)
        _send(cfg, "editMessageReplyMarkup", chat_id=chat, message_id=msg.get("message_id"),
              reply_markup={"inline_keyboard": _wallet_keyboard(flt)})
    elif data.startswith("conf:"):
        v = data.split(":")[1]
        if v == "none":
            flt["min_conf"] = 0
            _send(cfg, "answerCallbackQuery", callback_query_id=cb_id, text="把握度不限")
        else:
            try:
                c = int(v)
                flt["min_conf"] = c
                _send(cfg, "answerCallbackQuery", callback_query_id=cb_id, text=f"把握度 ≥{c} 分")
            except Exception:
                pass
        _save_flt(store, flt)
        _send(cfg, "editMessageReplyMarkup", chat_id=chat, message_id=msg.get("message_id"),
              reply_markup={"inline_keyboard": _conf_keyboard(flt)})
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
