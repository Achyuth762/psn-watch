"""Read-only dashboard served by the watcher itself (so it always shows live data).

Listens on 127.0.0.1 only; Caddy on the VM terminates HTTPS and achyuthus.dev/psn is a
Vercel rewrite to it. Everything is under /psn so the same paths work through the proxy.

  GET /psn                    the app (single page, installable to the home screen)
  GET /psn/api/status         live JSON: offers, store health, recent changes
  GET /psn/manifest.webmanifest, /psn/icon.svg, /psn/sw.js
  GET /psn/healthz            "ok" (for uptime checks)

Nothing here can change state or send alerts, so it is safe to leave public.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"
STALE_AFTER = 6 * 3600          # hide listings not re-checked for 6 h (removed by the store)


def snapshot(engine) -> dict:
    watched = {s["name"] for s in engine.stores}
    now = time.time()
    offers = [
        {k: o[k] for k in ("store", "title", "url", "status", "price", "denomination", "since", "checked")}
        for o in engine.db.offers()
        if o["store"] in watched and now - (o["checked"] or 0) < STALE_AFTER
    ]
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
    return {"now": now, "started": getattr(engine, "started", now), "offers": offers, "health": health, "skipped": skipped, "events": events,
            "alerts_on": bool(os.getenv("NTFY_TOPIC") or os.getenv("TELEGRAM_CHAT_ID"))}


def make_handler(engine):
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
                elif path == "/psn/healthz":
                    self._send(200, b"ok", "text/plain", "no-store")
                else:
                    self._send(404, b"not found", "text/plain", "no-store")
            except BrokenPipeError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("web request failed: %s", self.path)
                self._send(500, b"error", "text/plain", "no-store")

        def _file(self, name, ctype, cache):
            self._send(200, (STATIC / name).read_bytes(), ctype, cache)

    return Handler


def start(engine, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(engine))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    log.info("dashboard on http://%s:%d/psn", host, port)
    return srv
