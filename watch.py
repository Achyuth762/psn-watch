#!/usr/bin/env python3
"""psn-watch — always-on PSN India gift card stock watcher.

  python watch.py                 run forever (what the systemd service does)
  python watch.py --once          check every store once, print results, send alerts
  python watch.py --status        print what the running watcher last saw (from the database)
  python watch.py --test-notify   send a test restock alert to every configured channel
  python watch.py --only "Rooter,Games Kart" --once -v     debug specific stores
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from psnwatch import notify, report  # noqa: E402
from psnwatch.adapters import HTTP_ONLY  # noqa: E402
from psnwatch.db import DB  # noqa: E402
from psnwatch.engine import Engine  # noqa: E402
from psnwatch.fetch import Fetcher  # noqa: E402
from psnwatch.models import Offer  # noqa: E402


def load_env(*paths: Path):
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def pick_stores(cfg: dict, only: str | None, browser: bool) -> list[dict]:
    stores = [s for s in cfg["stores"] if s.get("enabled", True)]
    if only:
        wanted = {w.strip().lower() for w in only.split(",")}
        stores = [s for s in cfg["stores"] if s["name"].lower() in wanted]
    if os.getenv("PSN_ON_VM") == "1":
        skipped = [s["name"] for s in stores if s.get("blocks_cloud")]
        if skipped:
            logging.info("skipping stores that refuse cloud-server traffic: %s", ", ".join(skipped))
        stores = [s for s in stores if not s.get("blocks_cloud")]
    if not browser:
        stores = [s for s in stores if s["type"] in HTTP_ONLY or s.get("render", "auto") is not True]
    return stores


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--data", default=os.getenv("PSN_DATA", str(ROOT)), help="folder for db + status files")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--in-stock", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="never send alerts")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(threadName)-18s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    load_env(ROOT / ".env", Path("/etc/psn-watch.env"))
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    db = DB(str(data / "psnwatch.db"))

    if args.test_notify:
        if not notify.channels():
            sys.exit("No alert channel configured (NTFY_TOPIC / TELEGRAM_* in .env or /etc/psn-watch.env).")
        errs = notify.restock([Offer("psn-watch TEST", "Test alert: PSN India ₹1000", "https://store.playstation.com/en-in/",
                                     "in", 1000, 1000)])
        if errs:
            sys.exit("Failed: " + "; ".join(errs))
        print("Sent via:", ", ".join(notify.channels()))
        return

    if args.status:
        print(report.table(db.offers(), only_in=args.in_stock))
        return

    stores = pick_stores(cfg, args.only, browser=not args.no_browser)
    if not stores:
        sys.exit("No stores selected.")
    if not notify.channels() and not args.dry_run:
        logging.warning("No alert channel configured — alerts will only be printed.")
    fetcher = Fetcher(use_browser=not args.no_browser)
    engine = Engine(cfg, stores, fetcher, db, send_alerts=not args.dry_run)

    if args.once:
        http = [s for s in stores if s["type"] in HTTP_ONLY]
        other = [s for s in stores if s["type"] not in HTTP_ONLY]
        with ThreadPoolExecutor(8) as ex:
            results = list(ex.map(lambda s: (s, engine.run_store_once(s)), http))
        results += [(s, engine.run_store_once(s)) for s in other]
        for s, (offers, err) in results:
            engine.process(s, offers, err)
        report.write(engine, data)
        print(report.table(db.offers(), [h.to_dict() for h in engine.health.values()], args.in_stock))
        import time
        time.sleep(1.5)        # let alert threads finish
        return

    logging.info("watching %d stores: %s", len(stores), ", ".join(s["name"] for s in stores))
    engine.run_forever(report_fn=lambda e: report.write(e, data))


if __name__ == "__main__":
    main()
