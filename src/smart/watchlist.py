"""社区/手动推荐钱包管理（存储于 data/watchlist.json）。

这些钱包来自 X/Reddit 社区推荐或用户手动关注，不经过排行榜准入，
但仍参与做市商剔除。source: reddit/x/manual/custom。
"""
import json
import time
import threading
from pathlib import Path

_lock = threading.Lock()
DEFAULT = "data/watchlist.json"


def _load(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save(data: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2))


def add(path: str | Path, address: str, source: str = "manual", note: str = "") -> bool:
    """加入一个推荐钱包。返回是否新增。"""
    address = address.strip().lower()
    if not address.startswith("0x"):
        return False
    with _lock:
        data = _load(path)
        is_new = address not in data
        data[address] = {
            "source": source or "manual",
            "note": note.strip() or "",
            "added_ts": time.time(),
            "active": True,
        }
        _save(data, path)
    return is_new


def remove(path: str | Path, address: str) -> bool:
    """移除（停用）一个推荐钱包。"""
    address = address.strip().lower()
    with _lock:
        data = _load(path)
        if address not in data:
            return False
        data.pop(address, None)
        _save(data, path)
    return True


def active(path: str | Path) -> list[dict]:
    """返回所有活跃推荐钱包条目。"""
    data = _load(path)
    out = []
    for addr, item in data.items():
        if item.get("active", True):
            rec = dict(item)
            rec["address"] = addr
            out.append(rec)
    return out


def all(path: str | Path) -> list[dict]:
    data = _load(path)
    out = []
    for addr, item in data.items():
        rec = dict(item)
        rec["address"] = addr
        out.append(rec)
    return out
