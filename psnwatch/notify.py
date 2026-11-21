"""Alert channels, configured through environment variables (/etc/psn-watch.env on the VM):

  NTFY_TOPIC (+ optional NTFY_SERVER)     -> ntfy app push; restocks use priority 5, which
                                             rings through Do Not Disturb if you allow it
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   -> Telegram message
  DISCORD_WEBHOOK_URL                     -> Discord message
  HEALTHCHECK_URL                         -> pinged every minute (dead-man's switch, e.g. healthchecks.io)

Every channel is tried independently with retries, so one being down never blocks the others.
"""
from __future__ import annotations

import html
import logging
import os
import time

from urllib.parse import quote

import httpx

from .models import Offer


def _ascii(s: str) -> str:
    """HTTP headers must be ASCII: spell the rupee sign out and percent-encode URLs."""
    return s.replace("₹", "Rs ").encode("ascii", "ignore").decode().strip()


def _url(u: str) -> str:
    return quote(u, safe=":/?#[]@!$&'()*+,;=%~")

log = logging.getLogger(__name__)

RESTOCK, HEALTH, INFO = "restock", "health", "info"
_PRIORITY = {RESTOCK: "5", HEALTH: "4", INFO: "2"}
_TAGS = {RESTOCK: "rotating_light,video_game", HEALTH: "warning", INFO: "white_check_mark"}


def channels() -> list[str]:
    ch = []
    if os.getenv("NTFY_TOPIC"):
        ch.append("ntfy")
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        ch.append("telegram")
    if os.getenv("DISCORD_WEBHOOK_URL"):
        ch.append("discord")
    return ch


def price_str(o: Offer) -> str:
    if o.price is None:
        return ""
    s = f"₹{o.price:,.0f}"
    if o.markup_pct is not None and abs(o.markup_pct) >= 0.5:
        s += f" ({o.markup_pct:+.0f}%)"
    return s


def _retry(fn, what: str, attempts: int = 3) -> str | None:
    err = None
    for i in range(attempts):
        try:
            r = fn()
            if r.status_code < 300:
                return None
            err = f"HTTP {r.status_code} {r.text[:150]}"
            if 400 <= r.status_code < 500 and r.status_code != 429:
                break                       # bad token/topic: retrying won't help
        except httpx.HTTPError as e:
            err = str(e)
        time.sleep(2 * (i + 1))
    log.error("%s alert failed: %s", what, err)
    return f"{what}: {err}"


def send(title: str, body: str, kind: str = INFO, click: str | None = None,
         actions: list[tuple[str, str]] | None = None, html_body: str | None = None) -> list[str]:
    print(("\a" if kind == RESTOCK else "") + f"\n== {title}\n{body}\n", flush=True)
    errors = []
    with httpx.Client(timeout=15) as c:
        if os.getenv("NTFY_TOPIC"):
            server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
            headers = {"Title": _ascii(title) or "psn-watch",
                       "Priority": _PRIORITY[kind], "Tags": _TAGS[kind]}
            if click:
                headers["Click"] = _url(click)
            if actions:
                headers["Actions"] = "; ".join(f"view, {_ascii(label)[:30].replace(',', ' ')}, {_url(url)}"
                                              for label, url in actions[:3])
            e = _retry(lambda: c.post(f"{server}/{os.environ['NTFY_TOPIC']}", content=body.encode(), headers=headers),
                       "ntfy")
            if e:
                errors.append(e)
        if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
            text = f"<b>{html.escape(title)}</b>\n\n" + (html_body or html.escape(body))
            e = _retry(lambda: c.post(
                f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "parse_mode": "HTML",
                      "disable_web_page_preview": True, "disable_notification": kind == INFO,
                      "text": text[:4000]}), "telegram")
            if e:
                errors.append(e)
        if os.getenv("DISCORD_WEBHOOK_URL"):
            e = _retry(lambda: c.post(os.environ["DISCORD_WEBHOOK_URL"],
                                      json={"content": f"**{title}**\n{body}"[:1900]}), "discord")
            if e:
                errors.append(e)
    return errors


def restock(offers: list[Offer]) -> list[str]:
    reminder = all(o.reminder for o in offers)
    offers = sorted(offers, key=lambda o: (o.denomination or 0))
    stores = sorted({o.store for o in offers})
    vals = ", ".join(f"₹{o.denomination:,}" for o in offers if o.denomination) or f"{len(offers)} listing(s)"
    title = f"{'Still in stock' if reminder else 'IN STOCK'}: PSN {vals} @ {', '.join(stores)}"
    body = "\n".join(f"• {o.store}: {o.title} {price_str(o)}\n  {o.url}" for o in offers)
    html_body = "\n".join(f'• <b>{html.escape(o.store)}</b>: <a href="{html.escape(o.url)}">{html.escape(o.title)}</a> '
                          f"{html.escape(price_str(o))}" for o in offers)
    actions = [(f"Buy {('₹%s' % format(o.denomination, ',')) if o.denomination else ''} {o.store}".strip(), o.url)
               for o in offers[:3]]
    return send(title, body, RESTOCK, click=offers[0].url, actions=actions, html_body=html_body)


def ping_healthcheck(fail: bool = False):
    url = os.getenv("HEALTHCHECK_URL")
    if not url:
        return
    try:
        httpx.get(url.rstrip("/") + ("/fail" if fail else ""), timeout=10)
    except httpx.HTTPError as e:
        log.warning("healthcheck ping failed: %s", e)
