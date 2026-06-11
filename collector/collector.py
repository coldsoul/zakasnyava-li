#!/usr/bin/env python3
"""GTFS-RT collector — polls feeds every 20 s, writes zstd-compressed protobuf snapshots."""

import json
import logging
import os
import signal
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import zstandard as zstd
from google.transit import gtfs_realtime_pb2

SOFIA = ZoneInfo("Europe/Sofia")

FEEDS: dict[str, str] = {
    "tripupdates": "https://gtfs.sofiatraffic.bg/api/v1/trip-updates",
    "vehiclepositions": "https://gtfs.sofiatraffic.bg/api/v1/vehicle-positions",
    "alerts": "https://gtfs.sofiatraffic.bg/api/v1/alerts",
}
HEADERS = {"User-Agent": "zakasnyava-li/0.1 (https://github.com/coldsoul/zakasnyava-li)"}

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "20"))
WALL_CLOCK_TIMEOUT = int(os.environ.get("WALL_CLOCK_TIMEOUT", "45"))
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "data"))
TEXTFILE_DIR = Path(os.environ.get("TEXTFILE_DIR", "/var/lib/node_exporter/textfile_collector"))

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("collector")

_cctx = zstd.ZstdCompressor(level=3)
_shutdown = False


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------


def _handle_sigterm(signum: int, frame: object) -> None:
    global _shutdown
    log.warning(json.dumps({"event": "sigterm_received", "ts": time.time()}))
    _shutdown = True


def _handle_alarm(signum: int, frame: object) -> None:
    raise TimeoutError("wall-clock timeout exceeded")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def service_date(ts: float | None = None) -> str:
    """Return the GTFS service date (YYYY-MM-DD) for a Unix timestamp.

    Service day boundary is 04:00 Europe/Sofia — a 01:30 trip belongs to the
    previous calendar date.
    """
    dt = datetime.fromtimestamp(ts or time.time(), tz=SOFIA)
    if dt.hour < 4:
        dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def snapshot_path(feed: str, header_ts: int) -> Path:
    date = service_date(header_ts)
    directory = DATA_ROOT / "raw" / date / feed
    directory.mkdir(parents=True, exist_ok=True)
    hhmmss = datetime.fromtimestamp(header_ts, tz=UTC).strftime("%H%M%S")
    return directory / f"{hhmmss}.pb.zst"


def sd_notify(message: str) -> None:
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(notify_socket)
            sock.sendall(message.encode())
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Core I/O
# ---------------------------------------------------------------------------


def fetch_feed(url: str) -> tuple[int, bytes]:
    """Fetch URL with a hard wall-clock cap via SIGALRM.

    requests' read timeout resets on every received byte — a trickling server
    defeats it. SIGALRM fires unconditionally after WALL_CLOCK_TIMEOUT seconds.
    Must be called from the main thread (SIGALRM limitation).
    """
    signal.signal(signal.SIGALRM, _handle_alarm)
    signal.alarm(WALL_CLOCK_TIMEOUT)
    try:
        r = requests.get(url, headers=HEADERS, timeout=(10, 25))
        return r.status_code, r.content
    finally:
        signal.alarm(0)


def write_snapshot(path: Path, raw_pb: bytes) -> None:
    """Compress and write atomically: tmp file → rename.

    A SIGTERM mid-write leaves no partial file — the rename is atomic on POSIX.
    """
    compressed = _cctx.compress(raw_pb)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(compressed)
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def write_metrics(stats: dict) -> None:
    if not TEXTFILE_DIR.exists():
        return

    def gauge(name: str, help_text: str, values: dict[str, float]) -> list[str]:
        lines = [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
        for label, val in values.items():
            lines.append(f'{name}{{feed="{label}"}} {val}')
        return lines

    def counter(name: str, help_text: str, values: dict[str, float]) -> list[str]:
        lines = [f"# HELP {name} {help_text}", f"# TYPE {name} counter"]
        for label, val in values.items():
            lines.append(f'{name}{{feed="{label}"}} {val}')
        return lines

    lines: list[str] = []
    lines += gauge(
        "collector_last_poll_timestamp_seconds",
        "Unix timestamp of last poll attempt",
        stats["last_poll"],
    )
    lines += gauge(
        "collector_last_feed_header_timestamp_seconds",
        "Feed header.timestamp from last written snapshot",
        stats["last_header_ts"],
    )
    lines += counter(
        "collector_poll_failures_total", "Total fetch/parse failures per feed", stats["failures"]
    )
    lines += counter(
        "collector_snapshots_written_total", "Total snapshots written per feed", stats["snapshots"]
    )
    lines += [
        "# HELP collector_gap_seconds_max_24h Largest observed gap between snapshots in last 24 h",
        "# TYPE collector_gap_seconds_max_24h gauge",
        f"collector_gap_seconds_max_24h {stats['gap_max_24h']}",
    ]

    content = "\n".join(lines) + "\n"
    tmp = TEXTFILE_DIR / "collector.tmp.prom"
    tmp.write_text(content)
    tmp.rename(TEXTFILE_DIR / "collector.prom")


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


def poll_once(
    feed_name: str,
    url: str,
    last_header_ts: dict[str, int],
    stats: dict,
) -> None:
    stats["last_poll"][feed_name] = time.time()
    try:
        status, content = fetch_feed(url)
        if status != 200:
            raise ValueError(f"HTTP {status}")
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(content)
    except Exception as exc:
        stats["failures"][feed_name] += 1
        log.warning(
            json.dumps(
                {"event": "fetch_failed", "feed": feed_name, "error": str(exc), "ts": time.time()}
            )
        )
        return

    hts = feed.header.timestamp
    if hts and hts == last_header_ts.get(feed_name):
        return  # feed unchanged — dedup, skip write

    last_header_ts[feed_name] = hts
    stats["last_header_ts"][feed_name] = hts or 0

    ts = hts or int(time.time())
    path = snapshot_path(feed_name, ts)
    write_snapshot(path, content)
    stats["snapshots"][feed_name] += 1

    log.info(
        json.dumps(
            {
                "event": "snapshot_written",
                "feed": feed_name,
                "header_ts": hts,
                "entities": len(feed.entity),
                "bytes_raw": len(content),
                "path": str(path),
                "ts": time.time(),
            }
        )
    )


def run() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)

    last_header_ts: dict[str, int] = {}
    last_write_time: dict[str, float] = {}
    stats: dict = {
        "last_poll": {f: 0.0 for f in FEEDS},
        "last_header_ts": {f: 0 for f in FEEDS},
        "failures": {f: 0 for f in FEEDS},
        "snapshots": {f: 0 for f in FEEDS},
        "gap_max_24h": 0.0,
    }

    log.info(
        json.dumps(
            {
                "event": "startup",
                "feeds": list(FEEDS),
                "poll_interval": POLL_INTERVAL,
                "ts": time.time(),
            }
        )
    )
    sd_notify("READY=1")

    while not _shutdown:
        cycle_start = time.time()

        for feed_name, url in FEEDS.items():
            if _shutdown:
                break
            prev = stats["snapshots"][feed_name]
            poll_once(feed_name, url, last_header_ts, stats)
            if stats["snapshots"][feed_name] > prev:
                last_write_time[feed_name] = time.time()
            elif feed_name in last_write_time:
                gap = time.time() - last_write_time[feed_name]
                if gap > stats["gap_max_24h"]:
                    stats["gap_max_24h"] = gap
                if gap > 300:
                    log.warning(
                        json.dumps(
                            {
                                "event": "gap_detected",
                                "feed": feed_name,
                                "gap_seconds": round(gap),
                                "ts": time.time(),
                            }
                        )
                    )

        write_metrics(stats)
        sd_notify("WATCHDOG=1")

        # Sleep in 1-second increments so SIGTERM is handled promptly.
        deadline = cycle_start + POLL_INTERVAL
        while not _shutdown and time.time() < deadline:
            time.sleep(min(1.0, deadline - time.time()))

    log.info(json.dumps({"event": "shutdown_complete", "ts": time.time()}))


if __name__ == "__main__":
    run()
