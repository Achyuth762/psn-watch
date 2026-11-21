"""Generic product pages.

Stock is read in this order:
  1. schema.org JSON-LD `availability` (Flipkart, Play-Asia, MD Computers, most shops)
  2. text rules: an out-of-stock phrase wins over an in-stock phrase
If neither gives an answer the offer is "unknown", which never triggers an alert.

`listing` mode opens a category/search page first, collects product links that look
like PSN India cards, then checks each product page.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..match import clean, denomination, is_psn_india_card
from ..models import IN, OUT, UNKNOWN, Offer

OUT_DEFAULT = [r"out of stock", r"sold out", r"currently unavailable", r"notify me", r"coming soon",
               r"temporarily unavailable", r"no longer available"]
IN_DEFAULT = [r"add to cart", r"buy now", r"in stock"]

_IN_SCHEMA = ("instock", "limitedavailability", "onlineonly", "instoreonly")
_OUT_SCHEMA = ("outofstock", "soldout", "discontinued", "preorder", "presale", "backorder")


# ---------------------------------------------------------------- parsing helpers
def _jsonld_objects(soup: BeautifulSoup):
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                yield x
                for k in ("@graph", "offers", "hasVariant", "itemListElement", "item", "mainEntity"):
                    if k in x:
                        stack.append(x[k])


def jsonld_stock(soup: BeautifulSoup) -> tuple[str, float | None, str]:
    """Returns (status, price, name) from JSON-LD, status UNKNOWN if absent."""
    statuses, price, name = [], None, ""
    for o in _jsonld_objects(soup):
        t = str(o.get("@type", "")).lower()
        if "product" in t and not name:
            name = clean(str(o.get("name", "")))
        av = o.get("availability")
        if av:
            a = str(av).lower().rsplit("/", 1)[-1]
            if a in _IN_SCHEMA:
                statuses.append(IN)
            elif a in _OUT_SCHEMA:
                statuses.append(OUT)
            p = o.get("price") or o.get("lowPrice")
            try:
                if p not in (None, "", "0", 0) and price is None:
                    price = float(str(p).replace(",", ""))
            except ValueError:
                pass
    if IN in statuses:
        return IN, price, name
    if OUT in statuses:
        return OUT, price, name
    return UNKNOWN, price, name


def text_stock(text: str, out_rx: list[str], in_rx: list[str]) -> str:
    low = text.lower()
    if any(re.search(r, low) for r in out_rx):
        return OUT
    if any(re.search(r, low) for r in in_rx):
        return IN
    return UNKNOWN


def page_title(soup: BeautifulSoup) -> str:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return clean(og["content"])
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return clean(h1.get_text(" "))
    return clean(soup.title.get_text()) if soup.title else ""


def _visible_text(soup: BeautifulSoup, scope: str | None) -> str:
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    node = soup.select_one(scope) if scope else None
    return (node or soup).get_text("\n")


_PRICE_RX = re.compile(r"(?:₹|rs\.?)\s*([\d,]+(?:\.\d+)?)", re.I)


def parse_product(html: str, url: str, cfg: dict, fallback_title: str = "") -> Offer:
    soup = BeautifulSoup(html, "html.parser")
    status, price, ld_name = (UNKNOWN, None, "")
    if cfg.get("use_jsonld", True):
        status, price, ld_name = jsonld_stock(soup)
    title = fallback_title or ld_name or page_title(soup)
    note = "json-ld" if status != UNKNOWN else ""
    if status == UNKNOWN:
        text = _visible_text(soup, cfg.get("text_scope"))
        status = text_stock(text, cfg.get("out_patterns", OUT_DEFAULT), cfg.get("in_patterns", IN_DEFAULT))
        note = "text" if status != UNKNOWN else "couldn't read stock on page"
        if price is None:
            m = _PRICE_RX.search(text)
            if m:
                try:
                    price = float(m.group(1).replace(",", ""))
                except ValueError:
                    pass
    return Offer(cfg["name"], title, url, status, price, denomination(title), note)


def find_links(html: str, base_url: str, link_regex: str | None, require_psn: bool = True) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    rx = re.compile(link_regex, re.I) if link_regex else None
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        text = clean(a.get_text(" ")) or clean(a.get("title", ""))
        if rx and not rx.search(href):
            continue
        label = text or href
        if require_psn and not (is_psn_india_card(text) or is_psn_india_card(href.replace("-", " "))):
            continue
        if href not in out or len(label) > len(out[href]):
            out[href] = label
    return list(out.items())


# ---------------------------------------------------------------- adapter entry
def _discover(cfg, f, render) -> tuple[list[tuple[str, str]], list[Exception]]:
    found, errors = [], []
    for listing in cfg.get("listings", []):
        try:
            html = f.get_html(listing, render=render)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
            continue
        found.extend(find_links(html, listing, cfg.get("link_regex"))[: cfg.get("max_products", 20)])
    return found, errors


def check(cfg: dict, f, cache: dict | None = None) -> list[Offer]:
    """Listing pages are re-read every `discover_every` s (default 15 min); product pages every pass."""
    import time
    cache = {} if cache is None else cache
    render = cfg.get("render", "auto")
    offers: list[Offer] = []
    targets: list[tuple[str, str]] = [(u, "") for u in cfg.get("products", [])]
    listing_errors: list[Exception] = []
    if cfg.get("listings"):
        stale = time.time() - cache.get("discovered", 0) > cfg.get("discover_every", 900)
        if stale or not cache.get("links"):
            found, listing_errors = _discover(cfg, f, render)
            if found:
                cache["links"], cache["discovered"] = found, time.time()
            elif not listing_errors:
                cache["links"], cache["discovered"] = [], time.time()
        targets.extend(cache.get("links", []))
    if listing_errors and not targets:
        raise listing_errors[0]   # nothing else to check: report the store as failed
    if cfg.get("listings") and not targets:
        return [Offer(cfg["name"], "(no PSN cards listed)", cfg["listings"][0], UNKNOWN,
                      note="listing page had no PSN India card links — check the URL or link_regex")]
    seen = set()
    for url, label in targets:
        if url in seen:
            continue
        seen.add(url)
        try:
            html = f.get_html(url, render=render)
            o = parse_product(html, url, cfg, fallback_title=label if is_psn_india_card(label) else "")
        except Exception as e:  # one bad product page shouldn't hide the rest
            o = Offer(cfg["name"], label or url, url, UNKNOWN, note=f"error: {e}")
        offers.append(o)
    return offers
