"""Laptop -> VM relay: the laptop helper sends what it read so the dashboard can show it.

Authentication reuses the one secret both machines already share, the ntfy topic: requests are
signed with HMAC-SHA256 under a key derived from it, with a timestamp so a captured request can't
be replayed later. Someone who knew the topic could already read and send your alerts, so this
adds no new exposure, and the topic itself never travels in the request. The VM only accepts data
for stores marked blocks_cloud (the ones it can't read itself), and never sends alerts for them:
the laptop already did.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

log = logging.getLogger("psnwatch.relay")
MAX_SKEW = 300                       # seconds a signed request stays valid
OFFER_FIELDS = ("store", "title", "url", "status", "price", "denomination", "note")


def key_for(topic: str) -> bytes:
    return hashlib.sha256(b"psn-watch-ingest:" + topic.encode()).digest()


def sign(key: bytes, ts: str, body: bytes) -> str:
    return hmac.new(key, ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify(key: bytes, ts: str, sig: str, body: bytes, now: float | None = None) -> bool:
    try:
        if abs((now or time.time()) - float(ts)) > MAX_SKEW:
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(sign(key, ts, body), sig or "")


def payload(engine) -> dict:
    names = {s["name"] for s in engine.stores}
    offers = [{k: o[k] for k in OFFER_FIELDS if k in o} for o in engine.db.offers() if o["store"] in names]
    stores = []
    for st in engine.stores:
        h = engine.health[st["name"]]
        stores.append({"name": h.name, "ok": h.ok, "interval": int(h.interval), "last_ok": h.last_ok,
                       "last_run": h.last_run, "error": (h.last_error or "")[:160], "in_stock": h.in_stock,
                       "passes": h.passes, "kind": st["type"],
                       "history": [[x[0], 1 if x[1] else 0] for x in list(h.history)[-48:]]})
    return {"sent": time.time(), "stores": stores, "offers": offers}


def push(engine, url: str, topic: str) -> bool:
    import httpx
    body = json.dumps(payload(engine), ensure_ascii=False).encode()
    ts = str(int(time.time()))
    try:
        r = httpx.post(url, content=body, timeout=15, headers={
            "Content-Type": "application/json", "X-PSN-TS": ts, "X-PSN-Sig": sign(key_for(topic), ts, body)})
        if r.status_code != 200:
            log.warning("dashboard relay: HTTP %s %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:  # noqa: BLE001  (offline / asleep: try again next round)
        log.info("dashboard relay failed: %s", e)
        return False
