"""把 freebuff 的 SQLite 状态渲染成一份自包含的静态 HTML 仪表盘。

用于 Preview 预览：不需要服务器/端口/依赖，生成后直接用 htmlPath 打开。

用法:
    python scripts/preview_dashboard.py [db_path] [out_path]

默认: data/freebuff.db -> .freebuff/preview.html
"""
import datetime as _dt
import html
import json
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "freebuff.db")
DEFAULT_OUT = os.path.join(ROOT, ".freebuff", "preview.html")

TYPE_EMOJI = {"OPEN": "🟢", "ADD": "🟡", "REDUCE": "🔴", "SWEEP": "💸"}
TYPE_LABEL = {"OPEN": "新开仓", "ADD": "加仓", "REDUCE": "减仓/平仓", "SWEEP": "拆单建仓"}


def git_info():
    def run(*args):
        try:
            return subprocess.run(
                args, capture_output=True, text=True, cwd=ROOT, timeout=5
            ).stdout.strip()
        except Exception:
            return ""
    return {
        "branch": run("git", "rev-parse", "--abbrev-ref", "HEAD") or "?",
        "commit": run("git", "rev-parse", "--short", "HEAD") or "?",
    }


def fmt_usd(v):
    try:
        return f"${v:,.0f}"
    except (TypeError, ValueError):
        return "-"


def fmt_pct(v):
    try:
        return f"{v * 100:.0f}%"
    except (TypeError, ValueError):
        return "-"


def fmt_time(ts):
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def load(db_path):
    data = {
        "ok": False,
        "error": "",
        "wallets": [],
        "signals": [],
        "counts": {},
        "totals": {},
        "last_signal": None,
        "last_seed": None,
    }
    if not os.path.exists(db_path):
        data["error"] = f"数据库不存在: {db_path}"
        return data
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        data["error"] = str(e)
        return data

    try:
        data["wallets"] = [
            dict(r)
            for r in conn.execute(
                "SELECT name, address, score, win_rate, pnl, volume, closed_count,"
                " profit_factor, source FROM wallets WHERE active=1"
                " ORDER BY score DESC, pnl DESC LIMIT 100"
            )
        ]
        data["signals"] = [
            dict(r)
            for r in conn.execute(
                "SELECT created_at, type, wallet_name, title, outcome, usdc, price,"
                " notified FROM signals ORDER BY created_at DESC LIMIT 100"
            )
        ]
        data["counts"]["wallets"] = conn.execute(
            "SELECT COUNT(*) FROM wallets WHERE active=1"
        ).fetchone()[0]
        data["counts"]["signals"] = conn.execute(
            "SELECT COUNT(*) FROM signals"
        ).fetchone()[0]
        data["counts"]["notified"] = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE notified=1"
        ).fetchone()[0]
        data["counts"]["by_type"] = {
            r["type"]: r["n"]
            for r in conn.execute(
                "SELECT type, COUNT(*) n FROM signals GROUP BY type"
            )
        }
        row = conn.execute(
            "SELECT COALESCE(SUM(usdc),0) u, COUNT(DISTINCT address) w FROM signals"
        ).fetchone()
        data["totals"]["usdc"] = row["u"]
        data["totals"]["wallets"] = row["w"]
        row = conn.execute(
            "SELECT MAX(created_at) m FROM signals"
        ).fetchone()
        data["last_signal"] = row["m"]
        row = conn.execute(
            "SELECT value FROM meta WHERE key='last_seed_ts'"
        ).fetchone()
        if row:
            try:
                data["last_seed"] = float(row["value"])
            except ValueError:
                pass
        data["ok"] = True
    except sqlite3.Error as e:
        data["error"] = str(e)
    finally:
        conn.close()
    return data


def esc(s):
    return html.escape(str(s if s is not None else ""))


def wallet_rows(wallets):
    rows = []
    for i, w in enumerate(wallets, 1):
        rows.append(
            "<tr>"
            f"<td class='num'>{i}</td>"
            f"<td class='mono'>{esc(w['name'] or w['address'][:16])}"
            f"<span class='addr'>{esc(w['address'][:12])}…</span></td>"
            f"<td class='num'>{w['score']:.1f}</td>"
            f"<td class='num'>{fmt_pct(w['win_rate'])}</td>"
            f"<td class='num usd'>{fmt_usd(w['pnl'])}</td>"
            f"<td class='num usd'>{fmt_usd(w['volume'])}</td>"
            f"<td class='num'>{w['closed_count'] if w['closed_count'] is not None else '-'}</td>"
            f"<td class='src'>{esc(w['source'] or '-')}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def signal_rows(signals):
    rows = []
    for s in signals:
        t = s["type"] or "?"
        price_s = f"{s['price']:.3f}" if s["price"] is not None else "-"
        rows.append(
            "<tr>"
            f"<td class='num nowrap'>{fmt_time(s['created_at'])}</td>"
            f"<td class='nowrap'><span class='type'>{TYPE_EMOJI.get(t, '')}</span>"
            f"{esc(TYPE_LABEL.get(t, t))}</td>"
            f"<td class='mono'>{esc(s['wallet_name'] or '-')}</td>"
            f"<td class='title'>{esc(s['title'] or '-')}"
            f"<span class='outcome'>{esc(s['outcome'] or '')}</span></td>"
            f"<td class='num usd'>{fmt_usd(s['usdc'])}</td>"
            f"<td class='num'>{price_s}</td>"
            f"<td class='num'>{'📨' if s['notified'] else ''}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def render(data, git, generated_at, db_path):
    counts = data["counts"]
    by_type = counts.get("by_type", {})
    type_badges = "".join(
        f"<div class='card'><div class='k'>{TYPE_EMOJI.get(t, '')} "
        f"{TYPE_LABEL.get(t, t)}</div><div class='v'>{by_type.get(t, 0)}</div></div>"
        for t in ("OPEN", "ADD", "REDUCE", "SWEEP")
    )
    if not data["ok"]:
        body = f"<div class='error'>⚠️ {esc(data['error'])}</div>"
    else:
        body = (
            "<div class='cards'>"
            "<div class='card'><div class='k'>👛 活跃钱包</div>"
            f"<div class='v'>{counts.get('wallets', 0)}</div></div>"
            "<div class='card'><div class='k'>📡 信号总数</div>"
            f"<div class='v'>{counts.get('signals', 0)}</div></div>"
            "<div class='card'><div class='k'>📨 已推送</div>"
            f"<div class='v'>{counts.get('notified', 0)}</div></div>"
            "<div class='card'><div class='k'>💰 跟踪投注额</div>"
            f"<div class='v'>{fmt_usd(data['totals'].get('usdc'))}</div></div>"
            f"{type_badges}"
            "</div>"
            f"<div class='grid'>"
            "<section><h2>👛 活跃钱包（按评分）</h2>"
            "<table><thead><tr><th>#</th><th>钱包</th><th>评分</th><th>胜率</th>"
            "<th>PnL</th><th>成交量</th><th>已平仓</th><th>来源</th></tr></thead>"
            f"<tbody>{wallet_rows(data['wallets'])}</tbody></table></section>"
            "<section><h2>📡 最近信号</h2>"
            "<table><thead><tr><th>时间</th><th>类型</th><th>钱包</th><th>市场</th>"
            "<th>金额</th><th>价格</th><th>推送</th></tr></thead>"
            f"<tbody>{signal_rows(data['signals'])}</tbody></table></section>"
            "</div>"
        )
    info = []
    if data.get("last_signal"):
        info.append(f"最后信号: {fmt_time(data['last_signal'])}")
    if data.get("last_seed"):
        info.append(f"最后播种: {fmt_time(data['last_seed'])}")
    info_s = " · ".join(info) or "（数据库暂无数据）"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Freebuff-cloud 仪表盘</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #0d1117; color: #e6edf3;
  font: 14px/1.55 -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif; }}
.wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px 20px 60px; }}
header h1 {{ margin: 0 0 4px; font-size: 22px; }}
header .sub {{ color: #8b949e; }}
.meta {{ color: #8b949e; font-size: 12.5px; margin: 10px 0 18px; }}
.badges {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.badges span {{ background: #161b22; border: 1px solid #30363d; border-radius: 20px;
  padding: 2px 10px; font-size: 12px; color: #8b949e; font-family: ui-monospace, Consolas, monospace; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 14px 16px; }}
.card .k {{ color: #8b949e; font-size: 12.5px; }}
.card .v {{ font-size: 24px; font-weight: 700; margin-top: 4px; }}
.grid {{ display: grid; grid-template-columns: 1fr; gap: 20px; }}
section {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 14px 16px; overflow-x: auto; }}
h2 {{ margin: 0 0 10px; font-size: 15px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #21262d; white-space: nowrap; }}
th {{ color: #8b949e; font-weight: 600; position: sticky; top: 0; background: #161b22; }}
td.num, th {{ text-align: right; }}
td.title {{ white-space: normal; min-width: 220px; max-width: 380px; }}
td.title .outcome {{ display: block; color: #8b949e; font-size: 12px; }}
td.mono {{ font-family: ui-monospace, Consolas, monospace; }}
td .addr {{ color: #8b949e; font-size: 11px; margin-left: 6px; }}
td.usd {{ color: #7ee787; font-variant-numeric: tabular-nums; }}
td.src {{ color: #8b949e; font-size: 12px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; }}
.error {{ background: #3d1d1d; border: 1px solid #f85149; color: #ffa198; border-radius: 10px;
  padding: 14px 16px; margin-bottom: 20px; }}
footer {{ color: #484f58; font-size: 12px; margin-top: 26px; text-align: center; }}
code {{ background: #21262d; border-radius: 4px; padding: 1px 6px; font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Freebuff-cloud · Polymarket 聪明钱跟踪</h1>
    <div class="sub">排行榜播种 → 准入过滤 → 做市商剔除 → 0-100 评分 → 轮询监控 → Telegram 推送 + SQLite 入库</div>
  </header>
  <div class="meta">
    生成于 {generated_at}（静态快照，重启监控后重新生成） · {info_s}
    <div class="badges"><span>{esc(git['branch'])}</span><span>{esc(git['commit'])}</span>
    <span>{esc(db_path)}</span></div>
  </div>
  {body}
  <footer>重新生成: <code>python scripts/preview_dashboard.py</code></footer>
</div>
</body>
</html>
"""


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    out_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    data = load(db_path)
    git = git_info()
    generated_at = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_out = render(data, git, generated_at, db_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html_out)
    print(f"已生成 {out_path}（{len(html_out):,} 字节）")
    if not data["ok"]:
        print(f"警告: {data['error']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
