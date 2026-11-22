"""Tests for the always-on engine: fast paths, confirmation, health alerts, watchdog."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psnwatch import notify  # noqa: E402
from psnwatch.adapters import page, shopify, woo  # noqa: E402
from psnwatch.db import DB  # noqa: E402
from psnwatch.engine import Engine  # noqa: E402
from psnwatch.fetch import FetchError  # noqa: E402
from psnwatch.models import IN, OUT, Offer  # noqa: E402


class Rec:
    """Fake fetcher that records every request; responses come from callables or dicts."""
    def __init__(self, json_fn=None, html_fn=None):
        self.calls, self.json_fn, self.html_fn = [], json_fn, html_fn
        self.browser = None

    def get_json(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        return self.json_fn(url, params or {})

    def get_html(self, url, render="auto", wait_ms=0):
        self.calls.append((url, {}))
        return self.html_fn(url)


def woo_item(i, name, stock):
    return {"id": i, "name": name, "permalink": f"https://w/p{i}", "is_in_stock": stock,
            "prices": {"price": str(i * 100000), "currency_minor_unit": 2}}


# ------------------------------------------------------------------ WooCommerce fast path
def test_woo_discovers_then_uses_single_include_request():
    stock = {1: False, 2: False}

    def fn(url, p):
        items = [woo_item(1, "₹1000 PSN Gift Card", stock[1]), woo_item(2, "₹2000 PSN Gift Card", stock[2]),
                 woo_item(3, "PS5 Console", True)]
        if "include" in p:
            ids = {int(x) for x in p["include"].split(",")}
            return [i for i in items if i["id"] in ids]
        return items

    f, cache, cfg = Rec(fn), {}, {"name": "W", "url": "https://w"}
    assert len(woo.check(cfg, f, cache)) == 2
    assert all("search" in c[1] for c in f.calls)            # discovery = search queries
    f.calls.clear()
    stock[2] = True
    offers = woo.check(cfg, f, cache)
    assert len(f.calls) == 1 and f.calls[0][1]["include"] == "1,2"   # fast path: ONE request
    assert {o.denomination: o.status for o in offers} == {1000: OUT, 2000: IN}


def test_woo_vanished_product_triggers_rediscovery():
    items = [woo_item(1, "₹1000 PSN Gift Card", False), woo_item(2, "₹2000 PSN Gift Card", False)]
    f, cache, cfg = Rec(lambda u, p: items), {}, {"name": "W", "url": "https://w"}
    woo.check(cfg, f, cache)
    items.pop()                                               # product 2 unpublished
    woo.check(cfg, f, cache)
    assert cache["discovered"] == 0                           # next pass will search again


def test_woo_store_without_listings_stays_quiet():
    f, cache, cfg = Rec(lambda u, p: []), {}, {"name": "W", "url": "https://w", "discover_every": 300}
    woo.check(cfg, f, cache)
    n = len(f.calls)
    for _ in range(5):
        assert woo.check(cfg, f, cache) == []
    assert len(f.calls) == n                                  # no requests until next discovery


# ------------------------------------------------------------------ Shopify fast path
def test_shopify_fast_path_uses_product_js_in_paise():
    full = {"products": [{"title": "PlayStation Store Gift Card ₹1,000 – PSN Code", "handle": "psn-1000",
                          "variants": [{"id": 1, "available": False, "price": "1000.00"}]}]}

    def fn(url, p):
        if url.endswith("/products.json"):
            return full if p.get("page") == 1 else {"products": []}
        if url.endswith("/products/psn-1000.js"):
            return {"title": full["products"][0]["title"], "handle": "psn-1000",
                    "variants": [{"id": 1, "available": True, "price": 104900}]}
        raise FetchError("404", 404)

    f, cache, cfg = Rec(fn), {}, {"name": "S", "url": "https://s"}
    assert shopify.check(cfg, f, cache)[0].status == OUT
    f.calls.clear()
    o = shopify.check(cfg, f, cache)[0]
    assert [c[0] for c in f.calls] == ["https://s/products/psn-1000.js"]
    assert (o.status, o.price) == (IN, 1049.0)


# ------------------------------------------------------------------ page listing cache
def test_page_listing_is_cached_between_passes():
    listing = '<a href="/product/1">Rs.1000 PlayStation Store Gift Card</a>'
    product = '<h1>Rs.1000 PlayStation Store Gift Card</h1><p>Sold out</p>'
    f = Rec(html_fn=lambda u: listing if u.endswith("/list") else product)
    cfg, cache = {"name": "G", "listings": ["https://g/list"], "link_regex": "/product/"}, {}
    page.check(cfg, f, cache)
    page.check(cfg, f, cache)
    assert [u for u, _ in f.calls].count("https://g/list") == 1


# ------------------------------------------------------------------ engine
class Sent(list):
    pass


def make_engine(tmp_path, stores, fetcher, **settings):
    cfg = {"settings": {"down_after_min": 0, "drift_after_passes": 2, "remind_every_min": 0, **settings},
           "stores": stores}
    e = Engine(cfg, stores, fetcher, DB(str(tmp_path / "e.db")))
    e.sent = Sent()
    e._send = lambda fn, *a: e.sent.append((fn.__name__, a))
    return e


def test_engine_restock_alert_and_health_cycle(tmp_path):
    state = {"stock": False, "fail": False}

    def fn(url, p):
        if state["fail"]:
            raise FetchError("HTTP 403 for https://w", 403)
        return [woo_item(1, "₹1000 PSN Gift Card", state["stock"])]

    st = {"name": "W", "type": "woocommerce", "url": "https://w"}
    e = make_engine(tmp_path, [st], Rec(fn))
    e.process(st, *e.run_store_once(st))
    assert e.sent == []
    state["stock"] = True
    e.process(st, *e.run_store_once(st))
    assert e.sent[-1][0] == "restock" and e.sent[-1][1][0][0].denomination == 1000

    state["fail"] = True
    e.process(st, *e.run_store_once(st))                       # down_after=0 -> immediate health alert
    assert e.sent[-1][0] == "send" and "unreadable" in e.sent[-1][1][0]
    n = len(e.sent)
    e.process(st, *e.run_store_once(st))
    assert len(e.sent) == n                                    # alerted once, not every failure
    state["fail"] = False
    e.process(st, *e.run_store_once(st))
    assert "readable again" in e.sent[-1][1][0]
    assert e.health["W"].ok


def test_engine_drift_alert_when_listings_vanish(tmp_path):
    items = [woo_item(1, "₹1000 PSN Gift Card", False)]
    st = {"name": "W", "type": "woocommerce", "url": "https://w", "discover_every": 0}
    e = make_engine(tmp_path, [st], Rec(lambda u, p: list(items)))
    e.process(st, *e.run_store_once(st))
    items.clear()
    e.process(st, *e.run_store_once(st))
    e.process(st, *e.run_store_once(st))
    assert any("disappeared" in a[1][0] for a in e.sent)
    assert not e.health["W"].ok


def test_page_in_stock_must_be_confirmed(tmp_path):
    reads = iter(["<h1>Rs.1000 PlayStation Store Gift Card</h1><button>Add to cart</button>",
                  "<h1>Rs.1000 PlayStation Store Gift Card</h1><p>Sold out</p>"])
    st = {"name": "P", "type": "page", "products": ["https://p/1"]}
    e = make_engine(tmp_path, [st], Rec(html_fn=lambda u: next(reads)))
    t = time.time
    import psnwatch.engine as eng
    orig, eng.time.sleep = eng.time.sleep, (lambda s: None)    # skip the 3 s confirmation wait
    try:
        offers, err = e.run_store_once(st)
    finally:
        eng.time.sleep = orig
    e.process(st, offers, err)
    assert not any(a[0] == "restock" for a in e.sent)          # flicker didn't alert
    assert t                                                   # keep linters quiet


def test_page_in_stock_confirmed_alerts(tmp_path):
    html = "<h1>Rs.1000 PlayStation Store Gift Card</h1><button>Add to cart</button>"
    st = {"name": "P", "type": "page", "products": ["https://p/1"]}
    e = make_engine(tmp_path, [st], Rec(html_fn=lambda u: html))
    import psnwatch.engine as eng
    orig, eng.time.sleep = eng.time.sleep, (lambda s: None)
    try:
        e.process(st, *e.run_store_once(st))
    finally:
        eng.time.sleep = orig
    assert e.sent and e.sent[-1][0] == "restock"


def test_watchdog_detects_stalled_worker(tmp_path):
    st = {"name": "W", "type": "woocommerce", "url": "https://w"}
    e = make_engine(tmp_path, [st], Rec(lambda u, p: []), stall_after_min=20)
    assert e._stalled(time.time()) is None
    e.health["W"].last_beat -= 25 * 60
    assert "W" in e._stalled(time.time())


def test_heartbeat_text(tmp_path):
    st = {"name": "W", "type": "woocommerce", "url": "https://w"}
    e = make_engine(tmp_path, [st], Rec(lambda u, p: []))
    e.db.record([Offer("W", "PSN ₹1000", "u", IN, 1000, 1000)])
    title, body = e.heartbeat_text()
    assert "1/1 stores readable" in title and "In stock right now: 1" in body and "Restocks seen" in body


def test_startup_notice_rate_limited(tmp_path):
    st = {"name": "W", "type": "woocommerce", "url": "https://w"}
    e = make_engine(tmp_path, [st], Rec(lambda u, p: []))
    for _ in range(4):
        e._startup_notice()
    titles = [a[1][0] for a in e.sent]
    assert titles[0].startswith("psn-watch started") and "keeps restarting" in titles[1] and len(titles) == 2


# ------------------------------------------------------------------ notification formatting
def test_restock_message_headers_are_ascii(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200
        text = ""

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, content=None, headers=None, json=None):
            captured["headers"] = headers
            for v in headers.values():
                v.encode("ascii")                               # raises if not ASCII
            return FakeResp()

    monkeypatch.setenv("NTFY_TOPIC", "t")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(notify.httpx, "Client", FakeClient)
    errs = notify.restock([Offer("NI Store", "Rs. 4000 Sony PSN Wallet", "https://x.in/product/₹4000-psn/", IN, 4000, 4000)])
    assert errs == []
    h = captured["headers"]
    assert h["Priority"] == "5" and "%E2%82%B9" in h["Click"] and h["Title"].startswith("IN STOCK: PSN Rs 4,000")


def test_get_json_accepts_shopify_javascript_content_type():
    import httpx
    from psnwatch.fetch import Fetcher
    f = Fetcher(use_browser=False)
    f.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"content-type": "text/javascript; charset=utf-8"}, content=b'{"handle": "psn-1000"}')))
    assert f.get_json("https://s/products/psn-1000.js") == {"handle": "psn-1000"}
    f.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"<html>bot check</html>")))
    import pytest
    with pytest.raises(FetchError):
        f.get_json("https://s/x")


# ------------------------------------------------------------------ dashboard
def test_dashboard_serves_page_api_and_hides_unwatched_stores(tmp_path):
    import json
    import urllib.request
    from psnwatch import web
    st = {"name": "W", "type": "woocommerce", "url": "https://w"}
    cfg_stores = [st, {"name": "Blocked", "type": "page", "products": ["https://b/1"], "blocks_cloud": True}]
    e = make_engine(tmp_path, [st], Rec(lambda u, p: []))
    e.cfg["stores"] = cfg_stores
    e.db.record([Offer("W", "PSN ₹1000", "https://w/1", IN, 1000, 1000),
                 Offer("Gone", "PSN ₹2000", "https://g/1", OUT, 2000, 2000)])   # store no longer watched
    srv = web.start(e, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/psn").read().decode()
        assert "PSN India stock" in page
        r = urllib.request.urlopen(base + "/psn/api/status")
        assert "s-maxage=5" in r.headers["Cache-Control"]
        d = json.loads(r.read())
        assert [o["store"] for o in d["offers"]] == ["W"]
        assert d["skipped"] == [{"name": "Blocked", "url": "https://b/1"}]
        assert d["health"][0]["name"] == "W"
        sw = urllib.request.urlopen(base + "/psn/sw.js")
        assert sw.headers["Service-Worker-Allowed"] == "/psn"
        assert urllib.request.urlopen(base + "/psn/healthz").read() == b"ok"
        import urllib.error
        try:
            urllib.request.urlopen(base + "/etc/passwd")
            raise AssertionError("should 404")
        except urllib.error.HTTPError as err:
            assert err.code == 404
    finally:
        srv.shutdown()


def test_dashboard_reports_ui_version(tmp_path):
    import json
    import urllib.request
    from psnwatch import web
    st = {"name": "W", "type": "woocommerce", "url": "https://w"}
    e = make_engine(tmp_path, [st], Rec(lambda u, p: []))
    srv = web.start(e, "127.0.0.1", 0)
    try:
        d = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/psn/api/status").read())
        assert d["ui"] == web.ui_version() and len(d["ui"]) == 12
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ wishlist, history, settings
def test_wishlist_routes_matches_loud_and_others_quiet_once(tmp_path):
    from psnwatch import prefs
    st = {"name": "W", "type": "woocommerce", "url": "https://w"}
    e = make_engine(tmp_path, [st], Rec(lambda u, p: []), remind_every_min=0.0001)
    prefs.save(e.db, {"want": [1000], "max_markup": 5, "others": "quiet"})
    a = Offer("W", "PSN Rs 1000", "https://w/1", IN, 1020, 1000)      # +2% -> on the list
    b = Offer("W", "PSN Rs 2000", "https://w/2", IN, 2000, 2000)      # value not wanted
    c = Offer("W", "PSN Rs 1000 B", "https://w/3", IN, 1300, 1000)    # +30% -> too pricey
    e.process(st, [a, b, c], None)
    kinds = {k: [o.url for o in args[0]] for k, args in e.sent}
    assert kinds["restock"] == ["https://w/1"]
    assert sorted(kinds["restock_quiet"]) == ["https://w/2", "https://w/3"]
    e.sent.clear()
    e.db.conn.execute("UPDATE offers SET last_alert=0")            # pretend the reminder interval passed
    e.process(st, [Offer("W", "PSN Rs 1000", "https://w/1", IN, 1020, 1000),
                   Offer("W", "PSN Rs 2000", "https://w/2", IN, 2000, 2000)], None)
    assert [k for k, _ in e.sent] == ["restock"]                    # only the wanted one repeats


def test_wishlist_off_and_validation(tmp_path):
    import pytest as _pt
    from psnwatch import prefs
    st = {"name": "W", "type": "woocommerce", "url": "https://w"}
    e = make_engine(tmp_path, [st], Rec(lambda u, p: []))
    prefs.save(e.db, {"want": [500], "others": "off"})
    e.process(st, [Offer("W", "PSN Rs 2000", "https://w/2", IN, 2000, 2000)], None)
    assert not e.sent
    assert prefs.load(e.db) == {"want": [500], "max_markup": None, "others": "off"}
    for bad in ({"want": "1000"}, {"want": [True]}, {"max_markup": "x"}, {"others": "loudest"}, [1]):
        with _pt.raises(ValueError):
            prefs.clean(bad)
    assert prefs.matches({"want": [], "max_markup": None, "others": "quiet"}, None, None)


def test_item_history_spans_and_prices(tmp_path):
    db = DB(str(tmp_path / "h.db"))
    k = "S|https://s/1"
    db.record([Offer("S", "PSN Rs 1000", "https://s/1", OUT, 1100, 1000)], now=100)
    db.record([Offer("S", "PSN Rs 1000", "https://s/1", IN, 1050, 1000)], now=200)
    db.record([Offer("S", "PSN Rs 1000", "https://s/1", OUT, 1050, 1000)], now=500)
    db.record([Offer("S", "PSN Rs 1000", "https://s/1", IN, 1000, 1000)], now=900)
    h = db.item_history(k)
    assert h["spans"] == [[200, 500], [900, None]]
    assert h["prices"] == [[100, 1100], [200, 1050], [900, 1000]]
    assert [x["new"] for x in h["events"]] == ["out", "in", "out", "in"]


def test_settings_endpoints_need_the_key(tmp_path, monkeypatch):
    import json
    import urllib.error
    import urllib.request
    from urllib.parse import quote
    from psnwatch import web
    monkeypatch.setenv("PSN_ADMIN_KEY", "s3cret-key")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    sent = []
    monkeypatch.setattr(web.notify, "send", lambda *a, **k: sent.append(a) or [])
    st = {"name": "W", "type": "woocommerce", "url": "https://w"}
    e = make_engine(tmp_path, [st], Rec(lambda u, p: []))
    e.db.record([Offer("W", "PSN Rs 1000", "https://w/1", IN, 1300, 1000)], now=time.time())
    srv = web.start(e, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body=None, key=None):
        req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **({"X-PSN-Key": key} if key else {})})
        try:
            r = urllib.request.urlopen(req)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())
    try:
        assert post("/psn/api/prefs", {"want": [1000]})[0] == 403
        assert post("/psn/api/prefs", {"want": [1000]}, "wrong")[0] == 403
        assert post("/psn/api/key-check", key="s3cret-key") == (200, {"ok": True})
        code, d = post("/psn/api/prefs", {"want": [1000], "max_markup": 10, "others": "off"}, "s3cret-key")
        assert code == 200 and d["prefs"]["want"] == [1000]
        assert post("/psn/api/prefs", {"want": ["x"]}, "s3cret-key")[0] == 400
        s = json.loads(urllib.request.urlopen(base + "/psn/api/status").read())
        assert s["admin"] is True and s["prefs"]["max_markup"] == 10
        assert s["offers"][0]["match"] is False and s["offers"][0]["key"] == "W|https://w/1"   # +30% > 10%
        assert "s3cret" not in json.dumps(s)                                                    # key never leaks
        assert post("/psn/api/test", key="s3cret-key")[0] == 200 and sent
        assert post("/psn/api/test", key="s3cret-key")[0] == 429                                # rate-limited
        assert post("/psn/api/unlock")[0] == 503                                                # no ntfy topic set
        h = json.loads(urllib.request.urlopen(base + "/psn/api/item?key=" + quote("W|https://w/1")).read())
        assert h["spans"] and h["spans"][0][1] is None
        for _ in range(10):
            post("/psn/api/prefs", {}, "nope")
        assert post("/psn/api/key-check", key="s3cret-key")[0] == 429                           # locked out
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ one page, several face values (Stan Shop)
STAN_LD = {"@context": "https://schema.org", "@type": "Product", "name": "Playstation (PSN) Gift Card - INDIA", "sku": "1304",
           "offers": {"@type": "AggregateOffer", "priceCurrency": "INR", "offerCount": 4, "lowPrice": "1000", "highPrice": "4000",
                      "offers": [{"@type": "Offer", "sku": str(i + 1), "url": "https://www.stanshop.co/playstation-psn-gift-card-in-india",
                                  "priceCurrency": "INR", "price": str(v),
                                  "availability": "https://schema.org/" + ("InStock" if v == 2000 else "OutOfStock")}
                                 for i, v in enumerate((1000, 2000, 3000, 4000))]}}


def _stan_html(faces=(1000, 2000, 3000, 4000)):
    import json as _j
    ld = f'<script type="application/ld+json">{_j.dumps(STAN_LD)}</script>'
    cards = "".join(f"<div>₹{v:,} <span>Coupon Worth ₹{v}</span> SOLD OUT</div>" for v in faces)
    return f"<html><head><title>Buy PlayStation Gift Card India</title>{ld}{ld}</head><body>{cards}</body></html>"


def test_split_variants_one_offer_per_face_value():
    from psnwatch.adapters.page import check
    cfg = {"name": "Stan Shop", "type": "page", "render": False, "split_variants": True,
           "variant_face_regex": r"Coupon Worth ₹\s*([\d,]+)",
           "products": ["https://www.stanshop.co/playstation-psn-gift-card-in-india"]}

    class F:
        def get_html(self, url, render=False):
            return _stan_html()
    offers = check(cfg, F())
    assert [(o.denomination, o.status, o.price) for o in offers] == [
        (1000, OUT, 1000), (2000, IN, 2000), (3000, OUT, 3000), (4000, OUT, 4000)]     # deduped, in page order
    assert len({o.key for o in offers}) == 4
    assert offers[1].url.endswith("#2000") and offers[1].title.endswith("Rs 2,000")


def test_split_variants_falls_back_to_price_when_text_doesnt_line_up():
    from psnwatch.adapters.page import parse_variants
    offers = parse_variants(_stan_html(faces=(1000,)), "https://s/p", {"name": "S", "variant_face_regex": r"Coupon Worth ₹\s*([\d,]+)"})
    assert [o.denomination for o in offers] == [1000, 2000, 3000, 4000]
