#!/usr/bin/env python3
"""Build stop_events.sqlite from raw TripUpdates snapshots for one service date.

Usage:
    python pipeline/build_stop_events.py --date YYYY-MM-DD
"""

import argparse
import csv
import io
import logging
import os
import sqlite3
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

import zstandard as zstd
from google.transit import gtfs_realtime_pb2

SOFIA = ZoneInfo("Europe/Sofia")
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "data"))
DB_PATH = Path(os.environ.get("DB_PATH", "data/derived/stop_events.sqlite"))

LAST_PREDICTION_WINDOW = 30 * 60  # accept predictions up to 30 min after scheduled_ts
GHOST_WINDOW = 30 * 60  # no sighting within ±30 min of first departure = ghost
MAX_DELAY_SEC = 3 * 3600  # |delay| > 3 h = feed noise, discard and log
GHOST_RATE_WARN = 0.20  # per-line rate > 20% likely indicates a calendar-filtering bug

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("build_stop_events")

_dctx = zstd.ZstdDecompressor()

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stop_events (
  service_date  TEXT     NOT NULL,
  route_id      TEXT     NOT NULL,
  direction_id  INTEGER  NOT NULL,
  trip_id       TEXT     NOT NULL,
  stop_id       TEXT     NOT NULL,
  stop_sequence INTEGER  NOT NULL,
  scheduled_ts  INTEGER  NOT NULL,
  delay_sec     INTEGER,
  is_ghost      INTEGER  NOT NULL DEFAULT 0,
  PRIMARY KEY (service_date, trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS idx_se_route
  ON stop_events (route_id, direction_id, service_date);
"""


class StopTimeRow(NamedTuple):
    stop_sequence: int
    scheduled_ts: int
    stop_id: str


# ---------------------------------------------------------------------------
# GTFS loading
# ---------------------------------------------------------------------------


def gtfs_time_to_epoch(service_date_str: str, hms: str) -> int:
    """Convert GTFS HH:MM:SS to Unix epoch. HH may exceed 23 for overnight trips."""
    h, m, s = (int(x) for x in hms.split(":"))
    base = datetime.strptime(service_date_str, "%Y-%m-%d").replace(tzinfo=SOFIA)
    return int((base + timedelta(hours=h, minutes=m, seconds=s)).timestamp())


def _csv_rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))


def load_gtfs(
    zip_path: Path,
    service_date_str: str,
) -> tuple[
    dict[str, dict],  # active_trips: {trip_id: {route_id, direction_id}}
    dict[tuple[str, str], list[StopTimeRow]],  # schedule_map: {(trip_id, stop_id): [StopTimeRow]}
    dict[str, dict[int, StopTimeRow]],  # trip_all_stops: {trip_id: {seq: StopTimeRow}}
    dict[str, int],  # trip_first_dep: {trip_id: min_scheduled_ts}
]:
    zf = zipfile.ZipFile(zip_path)
    date_compact = service_date_str.replace("-", "")

    # Expand active service IDs for the date from calendar_dates.txt
    # (there is no calendar.txt — see SPEC.md §2 quirk 4)
    active_sids: set[str] = set()
    for row in _csv_rows(zf, "calendar_dates.txt"):
        if row["date"] == date_compact and row["exception_type"] == "1":
            active_sids.add(row["service_id"])

    if not active_sids:
        log.warning(f"No active services found for {service_date_str} in calendar_dates.txt")

    active_trips: dict[str, dict] = {}
    for row in _csv_rows(zf, "trips.txt"):
        if row["service_id"] in active_sids:
            active_trips[row["trip_id"]] = {
                "route_id": row["route_id"],
                "direction_id": int(row.get("direction_id") or 0),
            }

    active_trip_ids = set(active_trips)
    schedule_map: dict[tuple[str, str], list[StopTimeRow]] = defaultdict(list)

    for row in _csv_rows(zf, "stop_times.txt"):
        tid = row["trip_id"]
        if tid not in active_trip_ids:
            continue
        t = row.get("arrival_time") or row.get("departure_time")
        if not t:
            continue
        try:
            ts = gtfs_time_to_epoch(service_date_str, t)
        except (ValueError, KeyError):
            continue
        schedule_map[(tid, row["stop_id"])].append(
            StopTimeRow(
                stop_sequence=int(row.get("stop_sequence") or 0),
                scheduled_ts=ts,
                stop_id=row["stop_id"],
            )
        )

    for key in schedule_map:
        schedule_map[key].sort(key=lambda r: r.stop_sequence)

    # Flat index: trip_id → {stop_sequence: StopTimeRow} for output row generation
    trip_all_stops: dict[str, dict[int, StopTimeRow]] = defaultdict(dict)
    for (tid, _), rows in schedule_map.items():
        for sr in rows:
            trip_all_stops[tid].setdefault(sr.stop_sequence, sr)

    trip_first_dep: dict[str, int] = {}
    for tid, seqs in trip_all_stops.items():
        trip_first_dep[tid] = min(sr.scheduled_ts for sr in seqs.values())

    return active_trips, dict(schedule_map), dict(trip_all_stops), trip_first_dep


# ---------------------------------------------------------------------------
# Snapshot streaming
# ---------------------------------------------------------------------------


def iter_snapshots(snapshot_dir: Path):
    """Yield FeedMessage for each .pb.zst in the directory, sorted by filename."""
    for path in sorted(snapshot_dir.glob("*.pb.zst")):
        try:
            raw = _dctx.decompress(path.read_bytes())
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(raw)
            yield feed
        except Exception as exc:
            log.warning(f"Skipping corrupt/truncated snapshot {path.name}: {exc}")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def match(
    service_date_str: str,
    active_trips: dict[str, dict],
    schedule_map: dict[tuple[str, str], list[StopTimeRow]],
    trip_all_stops: dict[str, dict[int, StopTimeRow]],
    trip_first_dep: dict[str, int],
    snapshot_dir: Path,
) -> list[dict]:
    # (trip_id, stop_sequence) → (snapshot_hts, predicted_time_epoch)
    last_predictions: dict[tuple[str, int], tuple[int, int]] = {}
    trips_seen_near_start: set[str] = set()
    canceled_trips: set[str] = set()
    discarded_noise = 0

    for feed in iter_snapshots(snapshot_dir):
        hts = feed.header.timestamp or 0

        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue
            tu = entity.trip_update
            trip_id = tu.trip.trip_id

            if trip_id not in active_trips:
                continue

            # CANCELED entity → whole trip is ghost
            if tu.trip.schedule_relationship == 3:
                canceled_trips.add(trip_id)
                continue

            # Ghost detection window: was this trip sighted near its first departure?
            first_dep = trip_first_dep.get(trip_id)
            if first_dep is not None and abs(hts - first_dep) <= GHOST_WINDOW:
                trips_seen_near_start.add(trip_id)

            for stu in tu.stop_time_update:
                if stu.schedule_relationship == 1:  # SKIPPED stop → no event
                    continue

                rows_for_stop = schedule_map.get((trip_id, stu.stop_id))
                if not rows_for_stop:
                    continue  # RT stop not in static stop_times — drop

                predicted_time: int | None = None
                if stu.arrival.HasField("time"):
                    predicted_time = stu.arrival.time
                elif stu.departure.HasField("time"):
                    predicted_time = stu.departure.time
                if predicted_time is None:
                    continue

                # Loop-route disambiguation: pick scheduled stop closest to predicted_time
                best = min(rows_for_stop, key=lambda r: abs(r.scheduled_ts - predicted_time))

                if hts > best.scheduled_ts + LAST_PREDICTION_WINDOW:
                    continue  # prediction arrived after the 30-min window — discard

                key = (trip_id, best.stop_sequence)
                existing = last_predictions.get(key)
                # Strict >: keeps first prediction for same-hts duplicates (SPEC.md §7 Story 2)
                if existing is None or hts > existing[0]:
                    last_predictions[key] = (hts, predicted_time)

    rows: list[dict] = []

    for trip_id, trip_meta in active_trips.items():
        route_id = trip_meta["route_id"]
        direction_id = trip_meta["direction_id"]
        trip_is_ghost = trip_id in canceled_trips or trip_id not in trips_seen_near_start

        stop_seq_map = trip_all_stops.get(trip_id, {})
        for seq, sr in sorted(stop_seq_map.items()):
            if trip_is_ghost:
                rows.append(
                    {
                        "service_date": service_date_str,
                        "route_id": route_id,
                        "direction_id": direction_id,
                        "trip_id": trip_id,
                        "stop_id": sr.stop_id,
                        "stop_sequence": seq,
                        "scheduled_ts": sr.scheduled_ts,
                        "delay_sec": None,
                        "is_ghost": 1,
                    }
                )
            else:
                pred = last_predictions.get((trip_id, seq))
                if pred is None:
                    continue  # no prediction recorded for this stop
                _, predicted_time = pred
                delay_sec = predicted_time - sr.scheduled_ts
                if abs(delay_sec) > MAX_DELAY_SEC:
                    discarded_noise += 1
                    continue
                rows.append(
                    {
                        "service_date": service_date_str,
                        "route_id": route_id,
                        "direction_id": direction_id,
                        "trip_id": trip_id,
                        "stop_id": sr.stop_id,
                        "stop_sequence": seq,
                        "scheduled_ts": sr.scheduled_ts,
                        "delay_sec": delay_sec,
                        "is_ghost": 0,
                    }
                )

    if discarded_noise:
        log.warning(f"Discarded {discarded_noise} stop events with |delay| > 3 h (feed noise)")

    _check_ghost_rates(rows)
    return rows


def _check_ghost_rates(rows: list[dict]) -> None:
    route_trips: dict[str, set[str]] = defaultdict(set)
    ghost_trips: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        route_trips[row["route_id"]].add(row["trip_id"])
        if row["is_ghost"]:
            ghost_trips[row["route_id"]].add(row["trip_id"])
    for route_id, trips in route_trips.items():
        if not trips:
            continue
        rate = len(ghost_trips[route_id]) / len(trips)
        if rate > GHOST_RATE_WARN:
            log.warning(
                f"Route {route_id}: ghost rate {rate:.0%}"
                f" ({len(ghost_trips[route_id])}/{len(trips)}) — likely calendar-filtering bug"
            )


# ---------------------------------------------------------------------------
# Database write (idempotent)
# ---------------------------------------------------------------------------


def write_to_db(db_path: Path, service_date_str: str, rows: list[dict]) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    with con:
        con.executescript(_SCHEMA_SQL)
        con.execute("DELETE FROM stop_events WHERE service_date = ?", (service_date_str,))
        con.executemany(
            """
            INSERT INTO stop_events
              (service_date, route_id, direction_id, trip_id, stop_id,
               stop_sequence, scheduled_ts, delay_sec, is_ghost)
            VALUES
              (:service_date, :route_id, :direction_id, :trip_id, :stop_id,
               :stop_sequence, :scheduled_ts, :delay_sec, :is_ghost)
            """,
            rows,
        )
    con.close()
    return len(rows)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def find_gtfs_zip(service_date_str: str) -> Path:
    """Return the most recent GTFS zip at or before service_date."""
    candidates = sorted((DATA_ROOT / "gtfs").glob("*.zip"), reverse=True)
    for path in candidates:
        if path.stem <= service_date_str:
            return path
    raise FileNotFoundError(f"No GTFS zip found for {service_date_str} in {DATA_ROOT / 'gtfs'}")


def build(service_date_str: str) -> int:
    snapshot_dir = DATA_ROOT / "raw" / service_date_str / "tripupdates"
    if not snapshot_dir.exists():
        log.error(f"Snapshot directory not found: {snapshot_dir}")
        sys.exit(1)

    gtfs_zip = find_gtfs_zip(service_date_str)
    log.info(f"Using GTFS: {gtfs_zip.name}")

    active_trips, schedule_map, trip_all_stops, trip_first_dep = load_gtfs(
        gtfs_zip, service_date_str
    )
    log.info(f"Active trips for {service_date_str}: {len(active_trips)}")

    rows = match(
        service_date_str, active_trips, schedule_map, trip_all_stops, trip_first_dep, snapshot_dir
    )
    n = write_to_db(DB_PATH, service_date_str, rows)
    log.info(f"Wrote {n} stop_events rows for {service_date_str}")
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="Service date YYYY-MM-DD")
    args = ap.parse_args()
    build(args.date)
