"""Dashboard served by the watcher itself (so it always shows live data).

Listens on 127.0.0.1 only; Caddy on the VM terminates HTTPS and achyuthus.dev/psn is a
Vercel rewrite to it. Everything is under /psn so the same paths work through the proxy.

  GET /psn                    the app (single page, installable to the home screen)
  GET /psn/api/status         live JSON: offers, store health, recent changes
  GET /psn/manifest.webmanifest, /psn/icon.svg, /psn/sw.js
  GET /psn/healthz            "ok" (for uptime checks)
  GET /psn/api/item?key=...   one listing's history: stock spans, price changes

Changing anything needs the admin key (PSN_ADMIN_KEY in /etc/psn-watch.env), sent in the
X-PSN-Key header. A custom header can't be sent cross-site without a CORS preflight, which
this server never approves, so other sites can't drive these endpoints from your browser.
  POST /psn/api/prefs         save the wishlist          (key)
  POST /psn/api/test          send a test phone alert    (key)
  POST /psn/api/key-check     is this key right?         (key)
  POST /psn/api/unlock        send an unlock link to the phone alert channel (no key; rate-limited,
                              and the link only ever goes to your own ntfy topic)
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import notify, prefs

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"
STALE_AFTER = 6 * 3600          # hide listings not re-checked for 6 h (removed by the store)


def ui_version() -> str:
    """Fingerprint of the dashboard files; open pages reload themselves when it changes."""
    import hashlib
    h = hashlib.sha1()
    for name in ("index.html", "sw.js"):
        h.update((STATIC / name).read_bytes())
    return h.hexdigest()[:12]


UI_VERSION = ui_version()


def _markup(o: dict) -> float | None:
    if o.get("price") and o.get("denomination"):
        return round((o["price"] / o["denomination"] - 1) * 100, 1)
    return None


def snapshot(engine) -> dict:
    watched = {s["name"] for s in engine.stores}
    now = time.time()
    p = prefs.load(engine.db)
    offers = []
    for o in engine.db.offers():
        if o["store"] not in watched or now - (o["checked"] or 0) >= STALE_AFTER:
            continue
        d = {k: o[k] for k in ("key", "store", "title", "url", "status", "price", "denomination", "since", "checked")}
        d["match"] = prefs.matches(p, o["denomination"], _markup(o))
        offers.append(d)
    health = []
    for st in engine.stores:
        h = engine.health[st["name"]]
        hist = list(h.history)
        health.append({"name": h.name, "ok": h.ok, "interval": int(h.interval), "last_ok": h.last_ok,
                       "last_run": h.last_run, "error": h.last_error[:160], "in_stock": h.in_stock,
                       "passes": h.passes, "kind": st["type"],
                       "uptime": round(100 * sum(1 for x in hist if x[1]) / len(hist)) if hist else None,
                       "history": [[x[0], 1 if x[1] else 0] for x in hist],
                       "url": st.get("url") or (st.get("listings") or st.get("products") or [""])[0]})
    skipped = [{"name": s["name"], "url": s.get("url") or (s.get("listings") or s.get("products") or [""])[0]}
               for s in engine.cfg.get("stores", []) if s.get("enabled", True) and s["name"] not in watched]
    events = [{k: e[k] for k in ("ts", "store", "title", "url", "old", "new", "price", "denomination")}
              for e in engine.db.events(since=now - 30 * 86400, limit=400) if e["store"] in watched]
    return {"now": now, "ui": UI_VERSION, "started": getattr(engine, "started", now), "offers": offers, "health": health, "skipped": skipped, "events": events,
            "alerts_on": bool(os.getenv("NTFY_TOPIC") or os.getenv("TELEGRAM_CHAT_ID")),
            "prefs": p, "admin": bool(admin_key())}


def admin_key() -> str:
    return os.getenv("PSN_ADMIN_KEY", "").strip()


class Guard:
    """Rate limits for the write endpoints (one process, so in-memory is enough)."""
    def __init__(self):
        self.lock = threading.Lock()
        self.fails: list[float] = []
        self.last: dict[str, float] = {}

    def locked_out(self, now: float) -> bool:
        with self.lock:
            self.fails = [t for t in self.fails if now - t < 600]
            return len(self.fails) >= 10           # 10 wrong keys in 10 min -> refuse for a while

    def fail(self, now: float):
        with self.lock:
            self.fails.append(now)

    def allow(self, what: str, every: float, now: float) -> bool:
        with self.lock:
            if now - self.last.get(what, 0) < every:
                return False
            self.last[what] = now
            return True


def make_handler(engine, guard=None):
    guard = guard or Guard()

    class Handler(BaseHTTPRequestHandler):
        server_version = "psn-watch"

        def log_message(self, fmt, *args):      # keep journald quiet
            log.debug("web: " + fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str, cache: str, extra: dict | None = None):
            self.send_response(code)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            try:
                if path in ("/psn", "/"):
                    self._file("index.html", "text/html; charset=utf-8", "public, max-age=0, s-maxage=60")
                elif path == "/psn/api/status":
                    body = json.dumps(snapshot(engine), ensure_ascii=False).encode()
                    # short CDN cache: many phones refreshing still means ~1 request / 5 s to the VM
                    self._send(200, body, "application/json; charset=utf-8", "public, max-age=0, s-maxage=5")
                elif path == "/psn/manifest.webmanifest":
                    self._file("manifest.webmanifest", "application/manifest+json", "public, max-age=3600")
                elif path == "/psn/icon.svg":
                    self._file("icon.svg", "image/svg+xml", "public, max-age=86400")
                elif path == "/psn/sw.js":
                    self._send(200, (STATIC / "sw.js").read_bytes(), "text/javascript; charset=utf-8", "no-cache",
                               {"Service-Worker-Allowed": "/psn"})
                elif path == "/psn/api/item":
                    key = (parse_qs(urlsplit(self.path).query).get("key") or [""])[0]
                    if not key or len(key) > 2000:
                        return self._json(400, {"error": "missing key"})
                    self._json(200, engine.db.item_history(key), "public, max-age=0, s-maxage=30")
                elif path == "/psn/healthz":
                    self._send(200, b"ok", "text/plain", "no-store")
                else:
                    self._send(404, b"not found", "text/plain", "no-store")
            except BrokenPipeError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("web request failed: %s", self.path)
                self._send(500, b"error", "text/plain", "no-store")

        def _json(self, code: int, obj, cache: str = "no-store"):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", cache)

        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            now = time.time()
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 4096:
                    return self._json(413, {"error": "too large"})
                body = self.rfile.read(n) if n else b""
                if path == "/psn/api/unlock":
                    return self._unlock(now)
                key = admin_key()
                if not key:
                    return self._json(503, {"error": "settings are not set up on the server (PSN_ADMIN_KEY)"})
                if guard.locked_out(now):
                    return self._json(429, {"error": "too many wrong keys; try again in 10 minutes"})
                if not hmac.compare_digest(self.headers.get("X-PSN-Key", "").encode(), key.encode()):
                    guard.fail(now)
                    return self._json(403, {"error": "wrong key"})
                if path == "/psn/api/key-check":
                    return self._json(200, {"ok": True})
                if path == "/psn/api/prefs":
                    try:
                        p = prefs.save(engine.db, json.loads(body or b"{}"))
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    log.info("wishlist updated: %s", p)
                    return self._json(200, {"ok": True, "prefs": p})
                if path == "/psn/api/test":
                    if not guard.allow("test", 20, now):
                        return self._json(429, {"error": "wait 20 seconds between test alerts"})
                    p = prefs.load(engine.db)
                    vals = ", ".join(f"Rs {v:,}" for v in p["want"]) or "any face value"
                    errs = notify.send("Test alert: psn-watch can reach your phone",
                                       f"This is how a restock looks. Your list: {vals}"
                                       + (f", up to {p['max_markup']:+g}% over face value." if p["max_markup"] is not None else "."),
                                       notify.RESTOCK, click=public_url())
                    return self._json(200 if not errs else 502, {"ok": not errs, "errors": errs})
                return self._json(404, {"error": "not found"})
            except BrokenPipeError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("web POST failed: %s", self.path)
                self._json(500, {"error": "server error"})

        def _unlock(self, now: float):
            key = admin_key()
            if not key or not os.getenv("NTFY_TOPIC"):
                return self._json(503, {"error": "phone alerts or settings key aren't set up on the server"})
            if not guard.allow("unlock", 120, now):
                return self._json(429, {"error": "a link was sent in the last 2 minutes; check your phone"})
            # The key rides in the #fragment: browsers never send it to any server or log.
            link = f"{public_url()}#key={key}"
            errs = notify.send("Unlock psn-watch settings", "Tap to open the dashboard with settings unlocked on "
                               "this device. Ignore this if you didn't ask for it.", notify.QUIET, click=link,
                               actions=[("Unlock settings", link)])
            return self._json(200 if not errs else 502, {"ok": not errs})

        def _file(self, name, ctype, cache):
            self._send(200, (STATIC / name).read_bytes(), ctype, cache)

    return Handler


def public_url() -> str:
    return os.getenv("PSN_PUBLIC_URL", "https://achyuthus.dev/psn").rstrip("/")


def start(engine, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(engine))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    log.info("dashboard on http://%s:%d/psn", host, port)
    return srv
