"""Decide whether a product title is an India PSN wallet/gift card, and pull out its face value."""
from __future__ import annotations

import html
import re

_PSN = re.compile(r"\bpsn\b|playstation|\bps\s?(wallet|store)\b", re.I)
_CARD = re.compile(r"wallet|gift|card|top.?up|voucher|recharge|code", re.I)

# Things that mention PlayStation but are not wallet cards.
_NOT_CARD = re.compile(
    r"console|controller|dualsense|dualshock|memory card|portal|\bvr2?\b|headset|charging|dock|"
    r"repair|bundle|pre-?owned|\bdisc\b|\bstand\b|\bcover\b|\bskin\b|\bcase\b|"
    r"ps\s?plus|playstation plus|essential|extra|deluxe|premium|"
    r"steam|xbox|nintendo|roblox|fortnite|v-?bucks|valorant|google play|apple",
    re.I,
)

# Other regions. India cards are usually marked INR / India / Rs / ₹.
_OTHER_REGION = re.compile(
    r"\b(usd|us\$|nzd|aud|gbp|eur|aed|sar|qar|kwd|hkd|sgd|myr|idr|jpy|cad|thb|php|"
    r"us account|united states|uae|ksa|saudi|new zealand|australia|singapore|"
    r"hong kong|malaysia|indonesia|japan|canada|europe|uk\b)",
    re.I,
)

_DENOM = [
    re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]{3,6})", re.I),
    re.compile(r"([\d,]{3,6})\s*(?:₹|rs\.?|inr)\b", re.I),
    re.compile(r"\b(\d{3,5})\b"),
]


def clean(title: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(title or "")).strip()


def is_psn_india_card(title: str) -> bool:
    t = clean(title)
    if not (_PSN.search(t) and _CARD.search(t)):
        return False
    if _NOT_CARD.search(t):
        return False
    if _OTHER_REGION.search(t):
        return False
    return True


def denomination(title: str) -> int | None:
    t = clean(title)
    for rx in _DENOM:
        for m in rx.finditer(t):
            v = int(m.group(1).replace(",", ""))
            if 100 <= v <= 50000:
                return v
    return None
