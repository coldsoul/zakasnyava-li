#!/usr/bin/env python3
"""Download the daily static GTFS zip to data/gtfs/YYYY-MM-DD.zip."""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SOFIA = ZoneInfo("Europe/Sofia")
STATIC_URL = "https://gtfs.sofiatraffic.bg/api/v1/static"
HEADERS = {"User-Agent": "zakasnyava-li/0.1 (https://github.com/coldsoul/zakasnyava-li)"}
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "data"))
TIMEOUT = 120  # static zip is ~18.7 MB

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("fetch_static_gtfs")


def today_sofia() -> str:
    dt = datetime.now(SOFIA)
    if dt.hour < 4:
        dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def fetch(date: str | None = None) -> Path:
    date = date or today_sofia()
    dest = DATA_ROOT / "gtfs" / f"{date}.zip"

    if dest.exists():
        log.info(json.dumps({"event": "already_exists", "path": str(dest)}))
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info(json.dumps({"event": "downloading", "url": STATIC_URL, "dest": str(dest)}))
    t0 = time.time()

    r = requests.get(STATIC_URL, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()

    if r.content[:2] != b"PK":
        raise ValueError(f"response is not a ZIP (got {r.content[:4]!r})")

    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    tmp.rename(dest)

    log.info(
        json.dumps(
            {
                "event": "downloaded",
                "path": str(dest),
                "bytes": dest.stat().st_size,
                "elapsed_s": round(time.time() - t0, 1),
            }
        )
    )
    return dest


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD (default: today Sofia time)")
    args = ap.parse_args()
    fetch(args.date)
