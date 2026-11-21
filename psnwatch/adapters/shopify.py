"""Shopify stores.

Discovery  (every `discover_every` s): /products.json pages — finds every PSN listing.
Fast check (every pass):               /products/<handle>.js for each known PSN listing (~10 KB each)
"""
from __future__ import annotations

import time

from ..fetch import FetchError
from ..match import clean, denomination, is_psn_india_card
from ..models import IN, OUT, Offer


def _offers(cfg, base, p, price_in_paise: bool) -> list[Offer]:
    title = clean(p.get("title", ""))
    if not is_psn_india_card(title):
        return []
    url = f"{base}/products/{p['handle']}"
    variants = p.get("variants") or []

    def price(v):
        try:
            x = float(v["price"])
            return x / 100 if price_in_paise else x
        except (KeyError, TypeError, ValueError):
            return None

    if len(variants) <= 1:
        v = variants[0] if variants else {}
        return [Offer(cfg["name"], title, url, IN if v.get("available") else OUT, price(v), denomination(title))]
    return [Offer(cfg["name"], f"{title} – {v.get('title', '')}", f"{url}?variant={v['id']}",
                  IN if v.get("available") else OUT, price(v),
                  denomination(v.get("title", "")) or denomination(title)) for v in variants]


def discover(cfg, f) -> tuple[list[str], list[Offer]]:
    base = cfg["url"].rstrip("/")
    handles, offers = [], []
    for page in range(1, cfg.get("max_pages", 10) + 1):
        products = f.get_json(f"{base}/products.json", params={"limit": 250, "page": page}).get("products") or []
        if not products:
            break
        for p in products:
            got = _offers(cfg, base, p, price_in_paise=False)
            if got:
                handles.append(p["handle"])
                offers += got
    return handles, offers


def check(cfg: dict, f, cache: dict | None = None) -> list[Offer]:
    cache = {} if cache is None else cache
    base = cfg["url"].rstrip("/")
    now = time.time()
    if "handles" not in cache or now - cache.get("discovered", 0) > cfg.get("discover_every", 900):
        cache["handles"], offers = discover(cfg, f)
        cache["discovered"] = now
        return offers
    offers = []
    for h in cache["handles"]:
        try:
            p = f.get_json(f"{base}/products/{h}.js")
        except FetchError as e:
            if e.status == 404:          # listing removed or renamed: rediscover next pass
                cache["discovered"] = 0
                continue
            raise
        offers += _offers(cfg, base, p, price_in_paise=True)   # .js prices are in paise
    return offers
