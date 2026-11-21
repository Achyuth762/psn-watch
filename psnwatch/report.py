"""status.html / status.json (written every 30 s by the engine) and the terminal table."""
from __future__ import annotations

import html
import json
import time
from datetime import datetime
from pathlib import Path


def _ago(ts: float) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


def table(offers: list[dict], health: list[dict] | None = None, only_in: bool = False) -> str:
    rows = [o for o in offers if o["status"] == "in" or not only_in]
    lines = [f"{'STATUS':9} {'STORE':20} {'FACE':>7} {'PRICE':>8}  TITLE"]
    for o in rows:
        mark = "IN STOCK" if o["status"] == "in" else "out"
        lines.append(f"{mark:9} {o['store'][:20]:20} {('₹%s' % format(o['denomination'], ',')) if o['denomination'] else '':>7} "
                     f"{('₹%s' % format(round(o['price']), ',')) if o['price'] else '':>8}  {o['title'][:60]}")
        if o["status"] == "in":
            lines.append(f"{'':9} -> {o['url']}")
    if health:
        lines.append("")
        for h in health:
            state = "OK " if h["ok"] else "BAD"
            lines.append(f"[{state}] {h['name']:22} every {int(h['interval'])}s · last ok {_ago(h['last_ok'])}"
                         + (f" · {h['last_error'][:90]}" if h["last_error"] else ""))
    lines.append(f"\n{sum(1 for o in offers if o['status'] == 'in')} in stock · {len(offers)} listings tracked")
    return "\n".join(lines)


def write(engine, folder: Path):
    offers = engine.db.offers()
    health = [h.to_dict() for h in engine.health.values()]
    events = engine.db.events(since=time.time() - 7 * 86400, limit=50)
    folder = Path(folder)
    (folder / "status.json").write_text(json.dumps(
        {"generated": time.time(), "offers": offers, "health": health, "events": events},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "status.html").write_text(_html(offers, health, events, engine.tz), encoding="utf-8")


def _html(offers, health, events, tz) -> str:
    esc = html.escape
    n_in = sum(1 for o in offers if o["status"] == "in")
    orow = "".join(
        f'<tr class="{"in" if o["status"] == "in" else "out"}"><td><span class="pill">'
        f'{"In stock" if o["status"] == "in" else "Out"}</span></td><td>{esc(o["store"])}</td>'
        f'<td class="num">{("₹%s" % format(o["denomination"], ",")) if o["denomination"] else ""}</td>'
        f'<td class="num">{("₹%s" % format(round(o["price"]), ",")) if o["price"] else ""}</td>'
        f'<td><a href="{esc(o["url"])}" target="_blank" rel="noopener">{esc(o["title"])}</a></td>'
        f'<td>{_ago(o["since"])}</td></tr>' for o in offers)
    hrow = "".join(
        f'<tr class="{"in" if h["ok"] else "bad"}"><td><span class="pill">{"OK" if h["ok"] else "Problem"}</span></td>'
        f'<td>{esc(h["name"])}</td><td class="num">{int(h["interval"])}s</td><td>{_ago(h["last_ok"])}</td>'
        f'<td class="num">{h["in_stock"]}</td><td>{esc(h["last_error"][:120])}</td></tr>' for h in health)
    erow = "".join(
        f'<tr><td>{datetime.fromtimestamp(e["ts"], tz):%d %b %H:%M}</td><td>{esc(e["store"])}</td>'
        f'<td>{esc(e["title"])}</td><td>{esc(str(e["old"]))} → <b>{esc(e["new"])}</b></td></tr>' for e in events)
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30">
<title>{n_in} in stock · psn-watch</title><style>
body{{font:14px/1.45 system-ui,sans-serif;margin:20px;color:#1c1c1c;background:#fafafa}}
h1{{font-size:20px;margin:0}} h2{{font-size:15px;margin:22px 0 6px}} .sub{{color:#666}}
table{{border-collapse:collapse;width:100%;background:#fff}} td,th{{padding:6px 9px;border-bottom:1px solid #eee;text-align:left}}
th{{font-size:12px;color:#666}} .num{{text-align:right;white-space:nowrap}}
.pill{{padding:1px 8px;border-radius:10px;font-size:12px}} tr.in .pill{{background:#d7f5dd;color:#0a6b23}}
tr.out .pill{{background:#eee;color:#777}} tr.bad .pill{{background:#ffe0dc;color:#a3241a}} tr.out td{{color:#888}}
@media(prefers-color-scheme:dark){{body{{background:#161616;color:#e8e8e8}}table{{background:#1e1e1e}}td,th{{border-color:#2c2c2c}}a{{color:#8fb0ff}}}}
</style></head><body><h1>PSN India gift cards: {n_in} in stock</h1>
<div class="sub">Updated {datetime.now(tz):%d %b %Y %H:%M:%S}</div>
<h2>Listings</h2><table><tr><th>Status</th><th>Store</th><th class=num>Face</th><th class=num>Price</th><th>Listing</th><th>Since</th></tr>{orow}</table>
<h2>Store health</h2><table><tr><th></th><th>Store</th><th class=num>Every</th><th>Last OK</th><th class=num>In stock</th><th>Last error</th></tr>{hrow}</table>
<h2>Stock changes (7 days)</h2><table><tr><th>When</th><th>Store</th><th>Listing</th><th>Change</th></tr>{erow}</table>
</body></html>"""
