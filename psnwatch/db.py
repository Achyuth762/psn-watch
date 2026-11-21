"""SQLite state: current status of every listing, a log of every stock change, store health.

SQLite (WAL mode) survives crashes and restarts mid-write, unlike a JSON file, and the
events table becomes a restock history you can study later ("Games Kart restocks Mondays").
"""
from __future__ import annotations

import sqlite3
import threading
import time

from .models import IN, UNKNOWN, Offer

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers(
  key TEXT PRIMARY KEY, store TEXT, title TEXT, url TEXT, status TEXT, price REAL,
  denomination INTEGER, since REAL, checked REAL, last_alert REAL DEFAULT 0, last_error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS events(
  ts REAL, store TEXT, key TEXT, title TEXT, url TEXT, old TEXT, new TEXT, price REAL, denomination INTEGER);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ offers
    def record(self, offers: list[Offer], remind_after_min: float = 0,
               max_markup_pct: float | None = None, now: float | None = None) -> list[Offer]:
        """Store readings; return offers that should trigger a restock alert right now."""
        now = now or time.time()
        alerts = []
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN")
            for o in offers:
                row = cur.execute("SELECT status, since, last_alert FROM offers WHERE key=?", (o.key,)).fetchone()
                if o.status == UNKNOWN:
                    # An unreadable page says nothing about stock: keep the last good reading.
                    if row:
                        cur.execute("UPDATE offers SET last_error=? WHERE key=?", (o.note[:300], o.key))
                    continue
                prev_status, since, last_alert = row if row else (None, now, 0)
                if prev_status != o.status:
                    since = now
                    cur.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                                (now, o.store, o.key, o.title, o.url, prev_status, o.status, o.price, o.denomination))
                if o.status == IN and _markup_ok(o, max_markup_pct):
                    if prev_status != IN or (remind_after_min and now - last_alert >= remind_after_min * 60):
                        o.reminder = prev_status == IN
                        alerts.append(o)
                        last_alert = now
                cur.execute(
                    "INSERT INTO offers VALUES(?,?,?,?,?,?,?,?,?,?,'') ON CONFLICT(key) DO UPDATE SET "
                    "title=excluded.title, url=excluded.url, status=excluded.status, price=excluded.price, "
                    "denomination=excluded.denomination, since=excluded.since, checked=excluded.checked, "
                    "last_alert=excluded.last_alert, last_error=''",
                    (o.key, o.store, o.title, o.url, o.status, o.price, o.denomination, since, now, last_alert))
            cur.execute("COMMIT")
        return alerts

    def prev_status(self, key: str) -> str | None:
        with self.lock:
            r = self.conn.execute("SELECT status FROM offers WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def offers(self) -> list[dict]:
        with self.lock:
            cur = self.conn.execute("SELECT * FROM offers ORDER BY status='in' DESC, denomination, store")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def events(self, since: float = 0, limit: int = 200) -> list[dict]:
        with self.lock:
            cur = self.conn.execute("SELECT * FROM events WHERE ts>=? ORDER BY ts DESC LIMIT ?", (since, limit))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------------ small key/value store
    def get(self, k: str, default=None):
        with self.lock:
            r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set(self, k: str, v):
        with self.lock:
            self.conn.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def _markup_ok(o: Offer, max_markup_pct: float | None) -> bool:
    if max_markup_pct is None or o.markup_pct is None:
        return True
    return o.markup_pct <= max_markup_pct
