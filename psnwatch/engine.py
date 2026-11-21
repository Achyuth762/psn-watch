"""The always-on engine.

Every store gets its own worker thread with its own interval, so one slow or broken site
never delays the others:
  * WooCommerce / Shopify APIs: every ~20 s (one tiny request per pass)
  * pages read over plain HTTP: every ~60 s
  * pages that need Chromium: every 45–120 s (they share one browser, queued)

Robustness features:
  * jittered intervals; exponential backoff on errors; honours Retry-After on 429
  * page-based "in stock" readings are re-read once before alerting (no false alarms)
  * store down > N min, or a store whose PSN listings suddenly vanish -> health alert
  * daily heartbeat message, so silence means "nothing in stock", not "watcher dead"
  * watchdog: if any worker or the browser hangs, the process exits and systemd restarts it
  * optional dead-man's-switch ping (HEALTHCHECK_URL) every minute
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from . import notify
from .adapters import ADAPTERS, HTTP_ONLY
from .db import DB
from .fetch import FetchError, Fetcher
from .models import IN, UNKNOWN, Offer

log = logging.getLogger(__name__)


def default_interval(store: dict) -> float:
    if store["type"] in HTTP_ONLY:
        return 20
    if store.get("render", "auto") is True:
        return 90 if store["type"] == "page" else 45
    return 60


@dataclass
class Health:
    name: str
    interval: float
    last_beat: float = field(default_factory=time.time)
    last_ok: float = 0
    last_run: float = 0
    last_duration: float = 0
    failing_since: float = 0
    consecutive_failures: int = 0
    last_error: str = ""
    alerted_down: bool = False
    last_count: int = 0
    empty_streak: int = 0
    alerted_drift: bool = False
    in_stock: int = 0
    passes: int = 0
    # last 48 checks: (timestamp, ok, listings read) — drives the uptime bars
    history: deque = field(default_factory=lambda: deque(maxlen=48))

    @property
    def ok(self) -> bool:
        return self.consecutive_failures == 0 and not self.alerted_drift

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["history"] = [list(x) for x in self.history]
        d["ok"] = self.ok
        return d


class Engine:
    def __init__(self, cfg: dict, stores: list[dict], fetcher: Fetcher, db: DB, send_alerts: bool = True):
        self.cfg, self.stores, self.f, self.db = cfg, stores, fetcher, db
        s = cfg.get("settings", {})
        self.remind_min = s.get("remind_every_min", 10)
        self.max_markup = s.get("max_markup_pct")
        self.down_after = s.get("down_after_min", 20) * 60
        self.drift_after = s.get("drift_after_passes", 3)
        self.heartbeat_hour = s.get("heartbeat_hour", 9)
        self.tz = ZoneInfo(s.get("timezone", "Asia/Kolkata"))
        self.max_backoff = s.get("max_backoff_sec", 900)
        self.stall_after = s.get("stall_after_min", 20) * 60
        self.send_alerts = send_alerts
        self.health = {st["name"]: Health(st["name"], st.get("interval", default_interval(st))) for st in stores}
        self.cache: dict[str, dict] = {st["name"]: {} for st in stores}
        self.stop = threading.Event()
        self.started = time.time()

    # ------------------------------------------------------------------ one store, one pass
    def _check(self, store: dict) -> tuple[list[Offer], Exception | None]:
        try:
            return ADAPTERS[store["type"]](store, self.f, self.cache[store["name"]]), None
        except Exception as e:  # noqa: BLE001 — any store failure is data, never a crash
            return [], e

    def run_store_once(self, store: dict) -> tuple[list[Offer], Exception | None]:
        offers, err = self._check(store)
        if err or not store.get("confirm", store["type"] not in HTTP_ONLY):
            return offers, err
        new_in = [o for o in offers if o.status == IN and self.db.prev_status(o.key) != IN]
        if new_in:
            # Page-based reading says "in stock" for the first time: read again before alerting.
            time.sleep(3)
            again, err2 = self._check(store)
            second = {o.key: o.status for o in again}
            for o in new_in:
                if second.get(o.key) != IN:
                    log.info("%s: %s not confirmed on re-read (%s)", store["name"], o.title, second.get(o.key))
                    o.status = UNKNOWN
                    o.note = "in-stock reading not confirmed on re-read"
        return offers, err

    def process(self, store: dict, offers: list[Offer], err: Exception | None):
        h, now = self.health[store["name"]], time.time()
        h.last_run, h.passes = now, h.passes + 1
        readable = [o for o in offers if o.status != UNKNOWN]
        failed = err is not None or (offers and not readable)
        if failed:
            msg = str(err) if err else (offers[0].note or "couldn't read stock")
            self._on_failure(store, h, msg[:300], now)
        else:
            self._on_success(store, h, len(readable), now)
        alerts = self.db.record(offers, self.remind_min, self.max_markup, now)
        h.in_stock = sum(1 for o in readable if o.status == IN)
        h.history.append((round(now), not failed, len(readable)))
        if alerts:
            log.warning("%s %s: %s", "REMINDER" if all(o.reminder for o in alerts) else "RESTOCK",
                        store["name"], ", ".join(o.title for o in alerts))
            self._send(notify.restock, alerts)

    def _on_failure(self, store, h: Health, msg: str, now: float):
        h.consecutive_failures += 1
        h.last_error = msg
        if not h.failing_since:
            h.failing_since = now
        log.info("%s: failure #%d: %s", store["name"], h.consecutive_failures, msg)
        if not h.alerted_down and now - h.failing_since >= self.down_after:
            h.alerted_down = True
            mins = int((now - h.failing_since) / 60)
            self._send(notify.send, f"{store['name']} unreadable for {mins} min",
                       f"Last error: {msg}\nThe watcher keeps retrying with backoff. If this persists the site "
                       f"may be blocking the VM or its layout changed.", notify.HEALTH)

    def _on_success(self, store, h: Health, count: int, now: float):
        if h.alerted_down:
            self._send(notify.send, f"{store['name']} readable again",
                       f"Recovered after {int((now - h.failing_since) / 60)} min.", notify.INFO)
        h.consecutive_failures, h.failing_since, h.alerted_down, h.last_error = 0, 0, False, ""
        h.last_ok = now
        expects_listings = store.get("expect_listings", True)
        if count == 0 and h.last_count > 0 and expects_listings:
            h.empty_streak += 1
            if h.empty_streak >= self.drift_after and not h.alerted_drift:
                h.alerted_drift = True
                self._send(notify.send, f"{store['name']}: PSN listings disappeared",
                           f"It had {h.last_count} PSN listing(s); the last {h.empty_streak} reads found none. "
                           "Either the store removed them or its page changed and the reader needs a fix.",
                           notify.HEALTH)
            return                       # keep last_count so the drift stays visible
        if count > 0 and h.alerted_drift:
            self._send(notify.send, f"{store['name']}: PSN listings back", f"Found {count} again.", notify.INFO)
        h.empty_streak, h.alerted_drift = 0, False
        h.last_count = count if count else h.last_count

    def _send(self, fn, *args):
        if not self.send_alerts:
            return
        threading.Thread(target=fn, args=args, daemon=True, name="alert").start()

    # ------------------------------------------------------------------ worker loop
    def _worker(self, store: dict):
        h = self.health[store["name"]]
        base = h.interval
        backoff = 0.0
        self.stop.wait(random.uniform(0, min(base, 15)))      # stagger start-up
        while not self.stop.is_set():
            h.last_beat = t0 = time.time()
            offers, err = self.run_store_once(store)
            try:
                self.process(store, offers, err)
            except Exception:  # noqa: BLE001
                log.exception("processing %s failed", store["name"])
            h.last_duration = time.time() - t0
            h.last_beat = time.time()
            if h.consecutive_failures:
                backoff = min(self.max_backoff, max(base * 2, backoff * 2 if backoff else base * 2))
                delay = backoff
                if isinstance(err, FetchError) and err.retry_after:
                    delay = max(delay, err.retry_after)
            else:
                backoff = 0
                delay = base
            self.stop.wait(delay * random.uniform(0.85, 1.15))

    # ------------------------------------------------------------------ supervision
    def run_forever(self, report_fn=None):
        self._startup_notice()
        for st in self.stores:
            threading.Thread(target=self._worker, args=(st,), daemon=True, name=f"store:{st['name']}").start()
        last_ping = last_report = 0.0
        while not self.stop.is_set():
            now = time.time()
            stalled = self._stalled(now)
            if stalled:
                log.critical("watchdog: %s stalled — exiting so systemd restarts us", stalled)
                notify.ping_healthcheck(fail=True)
                logging.shutdown()
                os._exit(3)
            if now - last_ping >= 60:
                notify.ping_healthcheck()
                last_ping = now
            if report_fn and now - last_report >= 30:
                try:
                    report_fn(self)
                except Exception:  # noqa: BLE001
                    log.exception("report failed")
                last_report = now
            self._maybe_heartbeat()
            self.stop.wait(15)

    def _stalled(self, now: float) -> str | None:
        for h in self.health.values():
            if now - h.last_beat > max(self.stall_after, h.interval * 6):
                return f"store worker '{h.name}'"
        b = self.f.browser
        if b and not b.q.empty() and now - b.last_beat > 10 * 60:
            return "browser worker"
        return None

    def _startup_notice(self):
        now = time.time()
        last = float(self.db.get("last_start", 0) or 0)
        restarts = int(self.db.get("restarts_hour", 0) or 0)
        restarts = restarts + 1 if now - last < 3600 else 1
        self.db.set("last_start", now)
        self.db.set("restarts_hour", restarts)
        if restarts == 1:
            self._send(notify.send, f"psn-watch started: watching {len(self.stores)} stores",
                       "You'll get a daily heartbeat at %02d:00 and an urgent alert the moment a PSN India card "
                       "is in stock." % self.heartbeat_hour, notify.INFO)
        elif restarts == 3:
            self._send(notify.send, "psn-watch keeps restarting",
                       "3 restarts within an hour — check the VM logs (journalctl -u psn-watch).", notify.HEALTH)

    def _maybe_heartbeat(self):
        local = datetime.now(self.tz)
        today = local.strftime("%Y-%m-%d")
        if local.hour != self.heartbeat_hour or self.db.get("heartbeat_day") == today:
            return
        self.db.set("heartbeat_day", today)
        self._send(notify.send, *self.heartbeat_text(), notify.INFO)

    def heartbeat_text(self) -> tuple[str, str]:
        hs = list(self.health.values())
        bad = [h for h in hs if not h.ok]
        in_now = [o for o in self.db.offers() if o["status"] == IN]
        day = time.time() - 86400
        restocks = [e for e in self.db.events(since=day) if e["new"] == IN]
        title = f"psn-watch OK: {len(hs) - len(bad)}/{len(hs)} stores readable"
        lines = [f"In stock right now: {len(in_now)}", f"Restocks seen in last 24 h: {len(restocks)}"]
        lines += [f"• {e['store']}: {e['title']} at {datetime.fromtimestamp(e['ts'], self.tz):%H:%M}"
                  for e in restocks[:8]]
        if bad:
            lines.append("Not readable: " + "; ".join(f"{h.name} ({h.last_error[:60] or 'no listings'})" for h in bad))
        return title, "\n".join(lines)
