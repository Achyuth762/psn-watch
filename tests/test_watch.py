"""Offline tests. Fixtures mirror what the real stores returned on 17 Nov 2026."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psnwatch.adapters import page, shopify, variants_text, woo  # noqa: E402
from psnwatch.match import denomination, is_psn_india_card  # noqa: E402
from psnwatch.models import IN, OUT, UNKNOWN, Offer  # noqa: E402
from psnwatch.db import DB  # noqa: E402


class FakeFetcher:
    def __init__(self, json_map=None, html_map=None):
        self.json_map, self.html_map = json_map or {}, html_map or {}

    def get_json(self, url, params=None):
        q = (params or {}).get("search") or (params or {}).get("page")
        return self.json_map.get((url, q), self.json_map.get(url, []))

    def get_html(self, url, render="auto", wait_ms=0):
        if url not in self.html_map:
            raise Exception(f"404 {url}")
        return self.html_map[url]


# ------------------------------------------------------------------ matching
@pytest.mark.parametrize("title,ok", [
    ("PlayStation Network (PSN) Wallet Top-Up ₹5000 (India)", True),
    ("₹7000 PSN Gift Card", True),
    ("Rs. 4000 Sony PSN Wallet Top Up (Instant Delivery)", True),
    ("Sony PlayStation Gift Card Rs 3000 | PSN Wallet Recharge", True),
    ("PSN Card 1000 INR | Playstation Network India (India)", True),
    ("PS Wallet Top Up 2000 New for PS4, PS5", True),
    ("Rs.1000 PlayStation Store Gift Card (Instant Delivery)", True),
    ("Rs. 8000 PlayStation Network (Wallet Top Up / PSN Card) India Only", True),
    ("Sony PS5 PlayStation 5 Pro Console 2TB Digital (New)", False),
    ("Dual Charging Dock for PS4 Controller (PlayStation 4 Charging)", False),
    ("FMCB Ps2 memory card 8mb for sony PlayStation 2", False),
    ("PSN Card 100 USD | Playstation Network US", False),
    ("PSN Card 150 NZD | Playstation Network New Zealand", False),
    ("Steam Wallet Gift Card ₹150 INR – For Indian Accounts", False),
    ("Horizon Call of the Mountain VR Voucher Code PS5", False),
    ("PlayStation Plus Essential 3 Month Card", False),
    ("PlayStation PS5 (PS5 God Of War Ragnarok Digital Voucher Code) (Instant Delivery on WhatsApp)", False),
    ("PlayStation Gift Card India – PSN Code", True),
])
def test_is_psn_india_card(title, ok):
    assert is_psn_india_card(title) is ok


@pytest.mark.parametrize("title,d", [
    ("PlayStation Network (PSN) Wallet Top-Up ₹5000 (India)", 5000),
    ("PSN Card 1,500 INR | India", 1500),
    ("Rs. 4000 Sony PSN Wallet Top Up", 4000),
    ("PS Wallet Top Up 670 New for PS4, PS5", 670),
    ("Rs.1000 PlayStation Store Gift Card", 1000),
])
def test_denomination(title, d):
    assert denomination(title) == d


# ------------------------------------------------------------------ woocommerce
def test_woo_minor_units_and_filtering():
    base = "https://hgworld.in"
    items = [
        {"id": 1, "name": "Rs. 5000 PlayStation Network (Wallet Top Up / PSN Card) India Only",
         "permalink": base + "/shop/rs-5000/", "is_in_stock": True, "is_purchasable": True,
         "prices": {"price": "500000", "currency_minor_unit": 2}},
        {"id": 2, "name": "Horizon Call of the Mountain VR Voucher Code PS5", "permalink": base + "/x/",
         "is_in_stock": True, "prices": {"price": "392000", "currency_minor_unit": 2}},
        {"id": 3, "name": "₹1000 PSN Gift Card", "permalink": base + "/shop/1000/",
         "is_in_stock": False, "prices": {"price": "1000", "currency_minor_unit": 0}},
    ]
    f = FakeFetcher({(base + "/wp-json/wc/store/v1/products", "psn"): items})
    offers = woo.check({"name": "HG World", "url": base}, f)
    assert [(o.denomination, o.status, o.price) for o in offers] == [(5000, IN, 5000.0), (1000, OUT, 1000.0)]


# ------------------------------------------------------------------ shopify
def test_shopify_pages_and_variants():
    base = "https://www.simplygaming.in"
    p1 = {"products": [
        {"title": "PlayStation Store Gift Card ₹2,000 – PSN Code (Email Delivery)", "handle": "psn-2000",
         "variants": [{"id": 1, "title": "Default Title", "available": False, "price": "2000.00"}]},
        {"title": "PlayStation Store Gift Card India – PSN Code", "handle": "psn-multi",
         "variants": [{"id": 7, "title": "₹1000", "available": True, "price": "1050.00"},
                      {"id": 8, "title": "₹3000", "available": False, "price": "3100.00"}]},
        {"title": "Roblox Gift Card $25", "handle": "roblox", "variants": [{"id": 9, "available": True}]},
    ]}
    f = FakeFetcher({(base + "/products.json", 1): p1, (base + "/products.json", 2): {"products": []}})
    offers = shopify.check({"name": "Simply Gaming", "url": base}, f)
    got = sorted((o.denomination, o.status) for o in offers)
    assert got == [(1000, IN), (2000, OUT), (3000, OUT)]
    assert next(o for o in offers if o.denomination == 1000).markup_pct == 5.0


# ------------------------------------------------------------------ product pages
JSONLD_OOS = """<html><head><title>Buy Rs.1000 PlayStation Store Gift Card</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Rs.1000 PlayStation Store Gift Card",
"offers":{"@type":"Offer","price":"1000","priceCurrency":"INR","availability":"http://schema.org/OutOfStock"}}</script>
</head><body>Availability: Out Of Stock <button>Add to Cart</button></body></html>"""

JSONLD_IN = JSONLD_OOS.replace("OutOfStock", "InStock")

GTS_PRODUCT = """<html><head><title>Buy PS5, PS4 Games Online</title></head><body>
<h1>Rs.1000 PlayStation Store Gift Card (Instant Delivery)</h1><span>₹1,000</span>
<button disabled>1000 sold out</button><p>Out of Stock</p><button>Save for Later</button></body></html>"""

GTS_LISTING = """<html><body>
<a href="/product/2153a461?variant=09df">Rs.1000 PlayStation Store Gift Card (Instant Delivery)</a>
<a href="/product/f869a7ad">PlayStation Portal Remote Player</a>
<a href="/about">About</a></body></html>"""


def test_jsonld_beats_misleading_add_to_cart():
    cfg = {"name": "MD Computers", "products": ["https://md/p1"]}
    o = page.check(cfg, FakeFetcher(html_map={"https://md/p1": JSONLD_OOS}))[0]
    assert (o.status, o.price, o.denomination) == (OUT, 1000.0, 1000)
    o = page.check(cfg, FakeFetcher(html_map={"https://md/p1": JSONLD_IN}))[0]
    assert o.status == IN


def test_listing_discovery_and_text_rules():
    cfg = {"name": "Games The Shop", "listings": ["https://gts/playstation"], "link_regex": "/product/"}
    f = FakeFetcher(html_map={"https://gts/playstation": GTS_LISTING,
                              "https://gts/product/2153a461?variant=09df": GTS_PRODUCT})
    offers = page.check(cfg, f)
    assert len(offers) == 1                      # the Portal link is filtered out
    o = offers[0]
    assert (o.status, o.denomination, o.price) == (OUT, 1000, 1000.0)
    assert o.title.startswith("Rs.1000 PlayStation Store Gift Card")


def test_listing_with_no_matches_reports_unknown():
    cfg = {"name": "X", "listings": ["https://x/list"], "link_regex": "/product/"}
    o = page.check(cfg, FakeFetcher(html_map={"https://x/list": "<a href='/product/1'>Mouse</a>"}))[0]
    assert o.status == UNKNOWN and "no PSN" in o.note


def test_broken_product_page_is_unknown_not_crash():
    cfg = {"name": "X", "products": ["https://x/gone"]}
    o = page.check(cfg, FakeFetcher())[0]
    assert o.status == UNKNOWN and o.note.startswith("error")


# ------------------------------------------------------------------ rooter-style variants
ROOTER = """<html><body><h2>Select Options</h2>
<div>Rs.1000 Wallet Code</div><div>Out of Stock</div>
<div>Rs.2000 Wallet Code</div><div>₹2,050</div>
<div>Rs.3000 Wallet Code</div><div>Out of Stock</div>
<div>Apply offer for extra savings</div></body></html>"""


def test_variants_text():
    cfg = {"name": "Rooter", "url": "https://r/p", "item_regex": r"Rs\.?\s?(?P<denom>[\d,]+)\s*Wallet Code"}
    offers = variants_text.check(cfg, FakeFetcher(html_map={"https://r/p": ROOTER}))
    assert [(o.denomination, o.status) for o in offers] == [(1000, OUT), (2000, IN), (3000, OUT)]


# ------------------------------------------------------------------ db / alert logic
def test_alert_only_on_transition_and_markup_filter(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    a = Offer("S", "PSN ₹1000", "u1", OUT, 1000, 1000)
    assert db.record([a]) == []
    a.status = IN
    got = db.record([a])
    assert got == [a] and not got[0].reminder        # came back in stock -> alert
    assert db.record([a]) == []                      # still in stock -> quiet (no reminder configured)
    db.record([Offer("S", "PSN ₹1000", "u1", UNKNOWN, note="timeout")])
    assert db.prev_status("S|u1") == IN              # unreadable page keeps last good status
    assert db.record([a]) == []
    pricey = Offer("S", "PSN ₹2000", "u2", IN, 2600, 2000)   # +30%
    assert db.record([pricey], max_markup_pct=10) == []
    ev = db.events()
    assert [(e["old"], e["new"]) for e in reversed(ev) if e["key"] == "S|u1"] == [(None, OUT), (OUT, IN)]


def test_reminder(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    o = Offer("S", "PSN ₹1000", "u1", IN, 1000, 1000)
    assert db.record([o], remind_after_min=10, now=1000) == [o]
    o2 = Offer("S", "PSN ₹1000", "u1", IN, 1000, 1000)
    assert db.record([o2], remind_after_min=10, now=1000 + 5 * 60) == []
    got = db.record([o2], remind_after_min=10, now=1000 + 11 * 60)
    assert got == [o2] and got[0].reminder


def test_listing_failure_still_checks_pinned_products():
    cfg = {"name": "MD", "listings": ["https://md/list"], "link_regex": "/product/", "products": ["https://md/p1"]}
    offers = page.check(cfg, FakeFetcher(html_map={"https://md/p1": JSONLD_IN}))
    assert [o.status for o in offers] == [IN]
