"""Polymarket RTDS 实时数据客户端（ws-live-data.polymarket.com）。

订阅 activity/trades（全局每笔成交，免鉴权），payload 含 proxyWallet。
自动重连，回调式将每笔成交交给 handler(address, trade)。
"""
import json
import logging
import threading
import time

import websocket

logger = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"


class RtdsClient:
    def __init__(self, on_trade=None, on_status=None):
        """on_trade(address, trade_dict)；on_status(str)。均为可选回调。"""
        self.on_trade = on_trade
        self.on_status = on_status
        self._ws = None
        self._stop = threading.Event()
        self._thread = None
        self._connected = False

    def _status(self, msg):
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _connect(self):
        self._ws = websocket.create_connection(RTDS_URL, timeout=20)
        sub = {"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades", "filters": ""}]}
        self._ws.send(json.dumps(sub))
        self._connected = True
        self._status("connected")

    def _handle_msg(self, raw):
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except Exception:
            return
        if isinstance(obj, dict) and obj.get("payload"):
            payload = obj["payload"]
            addr = (payload.get("proxyWallet") or "").lower()
            if addr and self.on_trade:
                self.on_trade(addr, payload)

    # 静默超时：超过该秒数未收到任何数据则强制重连（防止半死连接挂起）
    _SILENCE_TIMEOUT = 120.0

    def _run(self):
        self._status("starting")
        last_data_ts = time.time()
        while not self._stop.is_set():
            try:
                if self._ws is None:
                    self._connect()
                    last_data_ts = time.time()
                self._ws.settimeout(1)
                try:
                    data = self._ws.recv()
                    if data:
                        last_data_ts = time.time()
                        self._handle_msg(data)
                except websocket.WebSocketTimeoutException:
                    # 心跳：RTDS 建议每 5s PING
                    try:
                        self._ws.ping("")
                    except Exception:
                        pass
                    # 静默检测：长时间无数据 → 强制重建连接（半死连接自救）
                    if time.time() - last_data_ts > self._SILENCE_TIMEOUT:
                        logger.warning("RTDS 静默 %.0fs 无数据，强制重连", time.time() - last_data_ts)
                        self._connected = False
                        self._status("silence-reconnect")
                        try:
                            self._ws.close()
                        except Exception:
                            pass
                        self._ws = None
                        time.sleep(2)
                        continue
                    continue
                except websocket.WebSocketConnectionClosedException:
                    self._connected = False
                    self._status("disconnected")
                    self._ws = None
                    # 退避重连
                    time.sleep(2)
                except Exception:
                    self._connected = False
                    self._ws = None
                    time.sleep(2)
            except Exception as e:
                logger.warning("RTDS 异常: %s", e)
                self._connected = False
                time.sleep(2)
        self._status("stopped")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=3)


# ======================================================================
# asset(clob token id) -> 市场信息 反查（补全 RTDS 缺失字段）
# ======================================================================
_asset_cache: dict = {}


def lookup_market_by_asset(asset: str) -> dict:
    """用 clob token id 反查市场信息（slug/conditionId/outcome/title）。
    带内存缓存。失败返回空 dict。"""
    if not asset:
        return {}
    if asset in _asset_cache:
        return _asset_cache[asset]
    import json as _json
    import urllib.parse
    from src.api.data_api import _get_session
    try:
        url = "https://gamma-api.polymarket.com/markets?" + urllib.parse.urlencode({"clob_token_ids": asset})
        resp = _get_session().get(url, timeout=8)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        if not data:
            return {}
        m = data[0]
        outcomes = m.get("outcomes") or []
        token_ids = m.get("clobTokenIds") or []
        if isinstance(outcomes, str):
            try: outcomes = _json.loads(outcomes)
            except Exception: outcomes = []
        if isinstance(token_ids, str):
            try: token_ids = _json.loads(token_ids)
            except Exception: token_ids = []
        token_ids = [str(t) for t in token_ids]
        # 找 asset 在 token_ids 的索引 → 对应 outcome
        outcome = ""
        if asset in token_ids:
            idx = token_ids.index(asset)
            if idx < len(outcomes):
                outcome = str(outcomes[idx])
        info = {
            "slug": m.get("slug") or "",
            "conditionId": m.get("conditionId") or "",
            "title": m.get("question") or "",
            "outcome": outcome,
        }
        _asset_cache[asset] = info
        return info
    except Exception:
        return {}
