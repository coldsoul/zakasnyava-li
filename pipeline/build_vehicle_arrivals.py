#!/usr/bin/env python3
"""Build vehicle_arrivals rows from raw VehiclePositions snapshots for one service date.

Usage:
    python pipeline/build_vehicle_arrivals.py --date YYYY-MM-DD
    python pipeline/build_vehicle_arrivals.py --date YYYY-MM-DD --gtfs PATH

Matching: VP entities carry (trip_id, stop_id) at 100% population (confirmed by
2026-07-01 recon). Same matching key as the TripUpdates matcher, so lookup is
a direct dict access. GPS geofencing is a secondary validation pass: flag rows
where the vehicle's position is far from the reported stop.

Ghost disproval: if a trip has no TripUpdates prediction but VP reports arrival
at its stops, the trip was NOT a ghost — delay_sec from VP overrides.
"""

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import zstandard as zstd
from google.transit import gtfs_realtime_pb2

try:
    from pipeline.build_stop_events import (
        StopTimeRow,
        find_gtfs_zip,
        load_gtfs,
        load_stop_coordinates,
    )
except ImportError:
    from build_stop_events import (  # noqa: F811
        StopTimeRow,
        find_gtfs_zip,
        load_gtfs,
        load_stop_coordinates,
    )

SOFIA = ZoneInfo("Europe/Sofia")
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "data"))
DB_PATH = Path(os.environ.get("DB_PATH", "data/derived/stop_events.sqlite"))

MAX_DELAY_SEC = 3 * 3600  # |delay| > 3 h = feed noise, discard and log
GPS_PLAUSIBILITY_M = 150  # flag as suspect if GPS > 150m from reported stop

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("build_vehicle_arrivals")

_dctx = zstd.ZstdDecompressor()
_SCHEMA_SQL = (Path(__file__).parent / "schemas.sql").read_text()


# ---------------------------------------------------------------------------
# GPS distance
# ---------------------------------------------------------------------------


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters between two (lat, lon) points."""
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Snapshot iterator
# ---------------------------------------------------------------------------


def iter_snapshots(snapshot_dir: Path):
    """Yield FeedMessage for each .pb.zst in sorted order."""
    for path in sorted(snapshot_dir.glob("*.pb.zst")):
        try:
            raw = _dctx.decompress(path.read_bytes())
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(raw)
            yield feed
        except Exception as exc:
            log.warning(f"Skipping corrupt/truncated snapshot {path.name}: {exc}")


# ---------------------------------------------------------------------------
# VP matching
# ---------------------------------------------------------------------------


def match_vp(
    service_date_str: str,
    active_trips: dict[str, dict],
    schedule_map: dict[tuple[str, str], list[StopTimeRow]],
    trip_all_stops: dict[str, dict[int, StopTimeRow]],
    stop_coords: dict[str, tuple[float, float]],
    snapshot_dir: Path,
) -> list[dict]:
    """Match VehiclePosition entities to scheduled stops.

    For each VP entity with trip_id and stop_id (100% populated per recon):
    1. Lookup (trip_id, stop_id) in schedule_map
    2. Loop route disambiguation: nearest scheduled time to measured_ts
    3. actual_arrival = measured_ts (vehicle's own clock)
    4. delay_sec = actual_arrival - scheduled_ts
    5. GPS validation: haversine(position, stop_coords[stop_id])
    """
    # (trip_id, stop_sequence) → (measured_ts, delay_sec, vehicle_id, gps_distance_m)
    arrivals: dict[tuple[str, int], tuple[int, int, str, float]] = {}
    unmatched_stops = 0
    discarded_noise = 0

    for feed in iter_snapshots(snapshot_dir):
        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue
            vp = entity.vehicle

            trip_id = vp.trip.trip_id if vp.HasField("trip") and vp.trip.trip_id else ""
            stop_id = vp.stop_id
            if not trip_id or not stop_id:
                continue

            if trip_id not in active_trips:
                continue

            # Lookup scheduled stop(s) for this (trip_id, stop_id)
            rows_for_stop = schedule_map.get((trip_id, stop_id))
            if not rows_for_stop:
                unmatched_stops += 1
                continue

            # measured_ts: when the vehicle reported this position
            measured_ts = vp.timestamp if vp.HasField("timestamp") and vp.timestamp else 0
            if not measured_ts:
                continue

            # Loop route disambiguation: pick nearest scheduled time
            best = min(rows_for_stop, key=lambda r: abs(r.scheduled_ts - measured_ts))

            delay_sec = measured_ts - best.scheduled_ts
            if abs(delay_sec) > MAX_DELAY_SEC:
                discarded_noise += 1
                continue

            # GPS distance validation
            gps_distance_m = -1.0
            if vp.HasField("position") and stop_id in stop_coords:
                stop_lat, stop_lon = stop_coords[stop_id]
                gps_distance_m = haversine_m(
                    vp.position.latitude,
                    vp.position.longitude,
                    stop_lat,
                    stop_lon,
                )

            vehicle_id = vp.vehicle.id if vp.HasField("vehicle") and vp.vehicle.id else ""

            key = (trip_id, best.stop_sequence)
            if key not in arrivals or measured_ts > arrivals[key][0]:
                # Keep the latest measurement per (trip, stop_sequence)
                arrivals[key] = (measured_ts, delay_sec, vehicle_id, gps_distance_m)

    # --- Build output rows ---
    rows: list[dict] = []
    for trip_id, trip_info in active_trips.items():
        stop_seq_map = trip_all_stops.get(trip_id, {})
        for seq, sr in sorted(stop_seq_map.items()):
            key = (trip_id, seq)
            if key in arrivals:
                _, delay_sec, vehicle_id, dist = arrivals[key]
                rows.append(
                    {
                        "service_date": service_date_str,
                        "route_id": trip_info["route_id"],
                        "direction_id": trip_info["direction_id"],
                        "trip_id": trip_id,
                        "stop_id": sr.stop_id,
                        "stop_sequence": seq,
                        "vehicle_id": vehicle_id,
                        "scheduled_ts": sr.scheduled_ts,
                        "actual_arrival": arrivals[key][0],
                        "delay_sec": delay_sec,
                        "gps_distance_m": round(dist, 1) if dist >= 0 else None,
                        "source": "vp",
                    }
                )

    log.info(f"Matched VP arrivals: {len(rows)} rows from {len(arrivals)} unique stops")
    if unmatched_stops:
        log.info(f"Unmatched stop_ids (not in schedule): {unmatched_stops}")
    if discarded_noise:
        log.info(f"Discarded noise (|delay| > 3 h): {discarded_noise}")

    # Flag GPS implausibility
    suspect = sum(
        1 for r in rows if r["gps_distance_m"] and r["gps_distance_m"] > GPS_PLAUSIBILITY_M
    )
    if suspect:
        log.warning(f"GPS implausible (> {GPS_PLAUSIBILITY_M}m from stop): {suspect} rows")

    return rows


# ---------------------------------------------------------------------------
# Database write (idempotent)
# ---------------------------------------------------------------------------


def write_to_db(db_path: Path, service_date_str: str, rows: list[dict]) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    with con:
        con.executescript(_SCHEMA_SQL)
        con.execute("DELETE FROM vehicle_arrivals WHERE service_date = ?", (service_date_str,))
        con.executemany(
            """
            INSERT INTO vehicle_arrivals
              (service_date, route_id, direction_id, trip_id, stop_id,
               stop_sequence, vehicle_id, scheduled_ts, actual_arrival,
               delay_sec, gps_distance_m, source)
            VALUES
              (:service_date, :route_id, :direction_id, :trip_id, :stop_id,
               :stop_sequence, :vehicle_id, :scheduled_ts, :actual_arrival,
               :delay_sec, :gps_distance_m, :source)
            """,
            rows,
        )
    con.close()
    return len(rows)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def build(service_date_str: str, gtfs_zip: Path | None = None) -> int:
    snapshot_dir = DATA_ROOT / "raw" / service_date_str / "vehiclepositions"
    if not snapshot_dir.exists():
        log.warning(
            f"Snapshot directory not found: {snapshot_dir} — skipping (no VP data collected yet)"
        )
        return 0

    gtfs_zip = gtfs_zip or find_gtfs_zip(service_date_str)
    log.info(f"Using GTFS: {gtfs_zip.name}")

    active_trips, schedule_map, trip_all_stops, _ = load_gtfs(gtfs_zip, service_date_str)
    stop_coords = load_stop_coordinates(gtfs_zip)
    log.info(f"Active trips: {len(active_trips)}, stops with coordinates: {len(stop_coords)}")

    rows = match_vp(
        service_date_str,
        active_trips,
        schedule_map,
        trip_all_stops,
        stop_coords,
        snapshot_dir,
    )

    written = write_to_db(DB_PATH, service_date_str, rows)
    log.info(f"Wrote {written} rows to vehicle_arrivals for {service_date_str}")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build vehicle_arrivals rows from VehiclePositions snapshots"
    )
    parser.add_argument("--date", required=True, help="Service date (YYYY-MM-DD)")
    parser.add_argument("--gtfs", type=Path, help="Path to GTFS static zip")
    args = parser.parse_args()

    start = datetime.now(tz=SOFIA)
    n = build(args.date, args.gtfs)
    elapsed = (datetime.now(tz=SOFIA) - start).total_seconds()

    log.info(
        json.dumps(
            {
                "script": "build_vehicle_arrivals",
                "service_date": args.date,
                "rows_written": n,
                "elapsed_s": round(elapsed, 1),
            }
        )
    )


if __name__ == "__main__":
    main()
