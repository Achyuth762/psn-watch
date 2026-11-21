"""WooCommerce stores: the public Store API returns stock status as JSON.

Discovery  (every `discover_every` s): GET /wp-json/wc/store/v1/products?search=psn  (a few queries)
Fast check (every pass):               GET /wp-json/wc/store/v1/products?include=12,34,56
The fast check is ONE small request for all known PSN products, so it's safe to run every
~20 seconds without hammering the shop.
"""
from __future__ import annotations

import time

from ..match import clean, denomination, is_psn_india_card
from ..models import IN, OUT, Offer

DEFAULT_QUERIES = ["psn", "playstation", "wallet top up"]
API = "/wp-json/wc/store/v1/products"


def _offer(cfg, p) -> Offer | None:
    name = clean(p.get("name", ""))
    if not is_psn_india_card(name):
        return None
    prices = p.get("prices") or {}
    price = None
    if prices.get("price"):
        price = int(prices["price"]) / (10 ** int(prices.get("currency_minor_unit", 2)))
    in_stock = bool(p.get("is_in_stock")) and p.get("is_purchasable", True)
    return Offer(cfg["name"], name, p.get("permalink", cfg["url"]), IN if in_stock else OUT, price, denomination(name))


def discover(cfg, f) -> dict[int, Offer]:
    base = cfg["url"].rstrip("/")
    found: dict[int, Offer] = {}
    for q in cfg.get("queries", DEFAULT_QUERIES):
        for p in f.get_json(base + API, params={"search": q, "per_page": 100}):
            o = _offer(cfg, p)
            if o and p.get("id") not in found:
                found[p["id"]] = o
    return found


def check(cfg: dict, f, cache: dict | None = None) -> list[Offer]:
    cache = {} if cache is None else cache
    base = cfg["url"].rstrip("/")
    now = time.time()
    if "ids" not in cache or now - cache.get("discovered", 0) > cfg.get("discover_every", 900):
        found = discover(cfg, f)
        cache["ids"], cache["discovered"] = sorted(found), now
        return list(found.values())
    if not cache["ids"]:                    # store has no PSN listing right now; wait for next discovery
        return []
    items = f.get_json(base + API, params={"include": ",".join(map(str, cache["ids"])), "per_page": 100})
    offers = [o for o in (_offer(cfg, p) for p in items) if o]
    if len(items) < len(cache["ids"]):      # a product vanished/unpublished: rediscover next pass
        cache["discovered"] = 0
    return offers
