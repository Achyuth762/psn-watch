"""Your wishlist: which face values you'd buy and the most you'd pay over face value.

Stored in the database (kv table) so it can be changed from the dashboard without a redeploy.
A restock that matches gets the loud, repeating alert; one that doesn't is sent once as a
quiet notification (or not at all, if you choose "off").
"""
from __future__ import annotations

import json

DEFAULT = {"want": [], "max_markup": None, "others": "quiet"}
OTHERS = ("quiet", "off", "loud")


def load(db) -> dict:
    try:
        p = json.loads(db.get("prefs") or "{}")
    except ValueError:
        p = {}
    return {**DEFAULT, **{k: v for k, v in p.items() if k in DEFAULT}}


def clean(raw: dict) -> dict:
    """Validate input from the dashboard; raises ValueError with a readable message."""
    if not isinstance(raw, dict):
        raise ValueError("expected a JSON object")
    want = raw.get("want", [])
    if not isinstance(want, list) or len(want) > 30:
        raise ValueError("want must be a list of face values")
    vals = []
    for v in want:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 100000:
            raise ValueError(f"bad face value: {v!r}")
        vals.append(int(v))
    mm = raw.get("max_markup")
    if mm is not None:
        if isinstance(mm, bool) or not isinstance(mm, (int, float)) or not -50 <= mm <= 200:
            raise ValueError("max_markup must be a number between -50 and 200, or null")
        mm = round(float(mm), 1)
    others = raw.get("others", "quiet")
    if others not in OTHERS:
        raise ValueError("others must be quiet, off or loud")
    return {"want": sorted(set(vals)), "max_markup": mm, "others": others}


def save(db, p: dict) -> dict:
    p = clean(p)
    db.set("prefs", json.dumps(p))
    return p


def matches(p: dict, denomination: int | None, markup_pct: float | None) -> bool:
    if p["want"] and denomination not in p["want"]:
        return False
    if p["max_markup"] is not None and markup_pct is not None and markup_pct > p["max_markup"]:
        return False
    return True
