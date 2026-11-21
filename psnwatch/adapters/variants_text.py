"""One page listing several denominations, each followed by its own stock label.

Rooter, for example, renders:
    Rs.1000 Wallet Code
    Out of Stock
    Rs.2000 Wallet Code
    ...
`item_regex` must have a named group `denom`. Each match's status is read from the
text between it and the next match.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..models import IN, OUT, Offer
from .page import OUT_DEFAULT


def check(cfg: dict, f, cache: dict | None = None) -> list[Offer]:
    url = cfg["url"]
    html = f.get_html(url, render=cfg.get("render", True), wait_ms=cfg.get("wait_ms", 3500))
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = re.sub(r"[ \t]+", " ", soup.get_text("\n"))
    rx = re.compile(cfg["item_regex"], re.I)
    out_rx = [re.compile(p, re.I) for p in cfg.get("out_patterns", OUT_DEFAULT)]
    matches = list(rx.finditer(text))
    offers = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), m.end() + 120)
        window = text[m.end():end]
        denom = int(m.group("denom").replace(",", ""))
        status = OUT if any(r.search(window) for r in out_rx) else IN
        offers.append(Offer(cfg["name"], f"{cfg.get('title_prefix', 'PSN India')} ₹{denom}", url,
                            status, None, denom))
    if not offers:
        offers.append(Offer(cfg["name"], "(no denominations found)", url,
                            note="item_regex matched nothing — page layout may have changed"))
    return offers
