#!/usr/bin/env python3
"""VP recon: inspect VehiclePositions feed to answer v2 design questions.

Questions answered by this script:
1. Is trip.trip_id populated in VehiclePosition entities?
2. Is stop_id or current_stop_sequence populated?
3. How do current_status values distribute?
4. What fields does a sample VP entity contain?

Output: a structured JSON report printed to stdout (redirect to save).

Data root is read from the DATA_ROOT env var (default: data/) so the script
works on the VPS where the collector writes to /var/lib/zakasnyava-li/data/.

Usage (local):                          Usage (VPS):
  python pipeline/tier2_vp_recon.py       DATA_ROOT=/var/lib/zakasnyava-li/data \\
    --date 2026-07-01                       python pipeline/tier2_vp_recon.py \\
    --gtfs data/gtfs/2026-07-01.zip           --date 2026-07-01 \\
                                              --gtfs /var/lib/zakasnyava-li/data/gtfs/2026-07-01.zip

Or use --data-root directly:
  python pipeline/tier2_vp_recon.py --date 2026-07-01 \\
      --data-root /var/lib/zakasnyava-li/data

If --gtfs is given, also computes VP/TU coverage ratio (active trips count).
Always produces the report; coverage ratio is optional in case GTFS zip is
not available.

Duration: typically < 30 s for 3 hours of VP data (~360 snapshots x ~590
entities each = ~200k entities). Memory: low (streaming).
"""

import argparse
import json
import os
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import zstandard as zstd
from google.transit import gtfs_realtime_pb2

SOFIA = ZoneInfo("Europe/Sofia")

FIELD_LABELS = {
    "trip": "HasField('trip')",
    "trip_id": "trip.trip_id",
    "stop_id": "stop_id",
    "current_stop_sequence": "current_stop_sequence",
    "vehicle": "HasField('vehicle')",
    "vehicle_id": "vehicle.id",
    "position": "HasField('position')",
    "lat_lon": "position.latitude AND longitude",
}

STATUS_LABEL = {
    0: "INCOMING_AT",
    1: "STOPPED_AT",
    2: "IN_TRANSIT_TO",
}

# ---------------------------------------------------------------------------
# Snapshot iterator
# ---------------------------------------------------------------------------


def iter_snapshots(snapshot_dir: Path):
    """Yield FeedMessage for each .pb.zst in sorted order."""
    if not snapshot_dir.is_dir():
        raise FileNotFoundError(f"Directory not found: {snapshot_dir}")

    paths = sorted(snapshot_dir.glob("*.pb.zst"))
    if not paths:
        raise FileNotFoundError(f"No .pb.zst files in {snapshot_dir}")

    dctx = zstd.ZstdDecompressor()
    for path in paths:
        try:
            raw = dctx.decompress(path.read_bytes())
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(raw)
            yield (path.name, feed)
        except Exception as exc:
            print(f"WARNING: skipping corrupt {path.name}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# VP entity inspection
# ---------------------------------------------------------------------------


def inspect_vp_entities(snapshot_dir: Path) -> dict:
    """Iterate all VP snapshots and accumulate field statistics.

    Returns a dict with:
      - snapshot_count, total_entities, first_ts, last_ts, duration_min
      - field_population: per-field populated/empty/pct
      - status_distribution: counts per current_status value
      - sample_entities: list of raw field dumps from first N distinct vehicles
    """
    field_counts: dict[str, Counter] = {k: Counter() for k in FIELD_LABELS}
    status_counts = Counter()
    snapshot_count = 0
    total_entities = 0
    first_ts: int | None = None
    last_ts: int | None = None
    sample_entities: list[dict] = []
    seen_vehicle_ids: set[str] = set()
    max_vehicles_per_snapshot = 0  # track peak VP count per snapshot

    for name, feed in iter_snapshots(snapshot_dir):
        snapshot_count += 1
        hts = feed.header.timestamp or 0
        if first_ts is None:
            first_ts = hts
        last_ts = hts

        vp_count_this_snapshot = 0

        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue

            vp = entity.vehicle
            vp_count_this_snapshot += 1
            total_entities += 1

            # --- Field presence checks ---
            _check(field_counts, "trip", vp.HasField("trip"))
            _check(field_counts, "trip_id", vp.HasField("trip") and bool(vp.trip.trip_id))
            _check(field_counts, "stop_id", bool(vp.stop_id))
            _check(
                field_counts,
                "current_stop_sequence",
                bool(vp.current_stop_sequence),
            )
            _check(field_counts, "vehicle", vp.HasField("vehicle"))
            _check(field_counts, "vehicle_id", vp.HasField("vehicle") and bool(vp.vehicle.id))
            _check(field_counts, "position", vp.HasField("position"))
            _check(
                field_counts,
                "lat_lon",
                vp.HasField("position")
                and vp.position.latitude != 0
                and vp.position.longitude != 0,
            )

            # --- Status distribution ---
            status_counts[vp.current_status] += 1

            # --- Sample entity (first N distinct vehicles) ---
            vid = vp.vehicle.id if vp.HasField("vehicle") and vp.vehicle.id else ""
            if vid and vid not in seen_vehicle_ids and len(sample_entities) < 10:
                seen_vehicle_ids.add(vid)
                sample_entities.append(_dump_vp_entity(vp, hts))

        max_vehicles_per_snapshot = max(max_vehicles_per_snapshot, vp_count_this_snapshot)

    # Build field_population summary with percentages
    field_population = {}
    for field, counter in field_counts.items():
        populated = counter[True]
        empty = counter[False]
        field_population[field] = {
            "label": FIELD_LABELS[field],
            "populated": populated,
            "empty": empty,
            "pct": round(populated / max(populated + empty, 1) * 100, 1),
        }

    # Build status distribution with labels
    status_distribution = {}
    for val, label in STATUS_LABEL.items():
        status_distribution[label] = status_counts.get(val, 0)
    # Catch any unexpected values
    for val, count in sorted(status_counts.items()):
        if val not in STATUS_LABEL:
            status_distribution[f"UNKNOWN_{val}"] = count

    duration_min = (
        round((last_ts - first_ts) / 60, 1)
        if (first_ts and last_ts and last_ts > first_ts)
        else None
    )

    return {
        "snapshot_count": snapshot_count,
        "total_entities": total_entities,
        "peak_vehicles_per_snapshot": max_vehicles_per_snapshot,
        "first_snapshot_ts": first_ts,
        "first_snapshot_iso": (
            datetime.fromtimestamp(first_ts, tz=SOFIA).isoformat() if first_ts else None
        ),
        "last_snapshot_ts": last_ts,
        "last_snapshot_iso": (
            datetime.fromtimestamp(last_ts, tz=SOFIA).isoformat() if last_ts else None
        ),
        "duration_minutes": duration_min,
        "field_population": field_population,
        "status_distribution": status_distribution,
        "sample_entities": sample_entities,
    }


def _check(counts: dict, field: str, condition: bool) -> None:
    counts[field][bool(condition)] += 1


def _dump_vp_entity(vp, snapshot_hts: int) -> dict:
    """Dump all populated fields of a VP entity for manual inspection."""
    d: dict = {"snapshot_header_ts": snapshot_hts}

    if vp.HasField("trip"):
        t = vp.trip
        d["trip"] = {}
        if t.trip_id:
            d["trip"]["trip_id"] = t.trip_id
        if t.route_id:
            d["trip"]["route_id"] = t.route_id
        if t.direction_id:
            d["trip"]["direction_id"] = t.direction_id
        if t.start_date:
            d["trip"]["start_date"] = t.start_date
        if t.start_time:
            d["trip"]["start_time"] = t.start_time
        d["trip"]["schedule_relationship"] = t.schedule_relationship
    else:
        d["trip"] = None

    if vp.HasField("vehicle"):
        v = vp.vehicle
        d["vehicle"] = {}
        if v.id:
            d["vehicle"]["id"] = v.id
        if v.label:
            d["vehicle"]["label"] = v.label
        if v.license_plate:
            d["vehicle"]["license_plate"] = v.license_plate
    else:
        d["vehicle"] = None

    if vp.HasField("position"):
        p = vp.position
        d["position"] = {
            "latitude": round(p.latitude, 6),
            "longitude": round(p.longitude, 6),
        }
        if p.bearing:
            d["position"]["bearing"] = p.bearing
        if p.speed:
            d["position"]["speed"] = p.speed
    else:
        d["position"] = None

    d["current_status"] = STATUS_LABEL.get(vp.current_status, f"UNKNOWN_{vp.current_status}")
    d["current_status_raw"] = vp.current_status

    if vp.stop_id:
        d["stop_id"] = vp.stop_id
    if vp.current_stop_sequence:
        d["current_stop_sequence"] = vp.current_stop_sequence
    if vp.HasField("timestamp") and vp.timestamp:
        d["measured_ts"] = vp.timestamp
        d["measured_iso"] = datetime.fromtimestamp(vp.timestamp, tz=SOFIA).isoformat()

    return d


# ---------------------------------------------------------------------------
# TripUpdates entity counting (for coverage ratio)
# ---------------------------------------------------------------------------


def count_tu_entities(tu_dir: Path) -> dict:
    """Count active trip entities in TripUpdates snapshots for the same period.

    Returns dict with snapshot_count, total_trip_entities, peak_trips_per_snapshot.
    """
    if not tu_dir or not tu_dir.is_dir():
        return {}

    dctx = zstd.ZstdDecompressor()
    snapshot_count = 0
    total_trip_entities = 0
    max_trips_per_snapshot = 0

    for path in sorted(tu_dir.glob("*.pb.zst")):
        try:
            raw = dctx.decompress(path.read_bytes())
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(raw)
            snapshot_count += 1
            trip_count = sum(1 for e in feed.entity if e.HasField("trip_update"))
            total_trip_entities += trip_count
            max_trips_per_snapshot = max(max_trips_per_snapshot, trip_count)
        except Exception as exc:
            print(f"WARNING: skipping corrupt TU {path.name}: {exc}", file=sys.stderr)

    return {
        "snapshot_count": snapshot_count,
        "total_trip_entities": total_trip_entities,
        "peak_trips_per_snapshot": max_trips_per_snapshot,
    }


# ---------------------------------------------------------------------------
# GTFS active trip count (for coverage ratio)
# ---------------------------------------------------------------------------


def count_active_trips(gtfs_zip_path: Path, service_date_str: str) -> int:
    """Count trips active on the given service date from calendar_dates.txt.

    Mirrors the logic in build_stop_events.load_gtfs().
    """
    import csv
    import io

    date_compact = service_date_str.replace("-", "")
    zf = zipfile.ZipFile(gtfs_zip_path)

    # Resolve active service IDs
    active_sids: set[str] = set()
    with zf.open("calendar_dates.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
            if row["date"] == date_compact and row["exception_type"] == "1":
                active_sids.add(row["service_id"])

    if not active_sids:
        return 0

    # Count trips in active services
    count = 0
    with zf.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
            if row["service_id"] in active_sids:
                count += 1

    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="VP recon: inspect VehiclePositions feed")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--date", help="Service date (YYYY-MM-DD), looks in data/raw/DATE/")
    src.add_argument(
        "--dir",
        type=Path,
        help="Direct path to vehiclepositions/ snapshot directory",
    )

    parser.add_argument(
        "--tu-dir",
        type=Path,
        help="Path to tripupdates/ snapshot directory for coverage ratio (default: same date)",
    )
    parser.add_argument(
        "--gtfs",
        type=Path,
        help="Path to GTFS static zip for active trip count (coverage ratio)",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("DATA_ROOT", "data")),
        help="Root data directory (default: data/)",
    )
    args = parser.parse_args()

    # Resolve snapshot directory
    if args.date:
        vp_dir = args.data_root / "raw" / args.date / "vehiclepositions"
        if not args.tu_dir:
            tu_dir = args.data_root / "raw" / args.date / "tripupdates"
        else:
            tu_dir = args.tu_dir
    else:
        vp_dir = args.dir
        tu_dir = args.tu_dir

    # Ensure VP dir exists before starting
    if not vp_dir.is_dir():
        print(f"ERROR: VehiclePositions directory not found: {vp_dir}", file=sys.stderr)
        sys.exit(1)

    start = time.time()

    # --- VP analysis ---
    result = inspect_vp_entities(vp_dir)
    result["recon_time_iso"] = datetime.now(tz=SOFIA).isoformat()
    result["vp_dir"] = str(vp_dir)
    result["elapsed_seconds"] = round(time.time() - start, 1)

    # --- TU comparison (optional) ---
    if tu_dir and tu_dir.is_dir():
        result["tu_comparison"] = count_tu_entities(tu_dir)
    else:
        if tu_dir:
            print(f"WARNING: TU directory not found: {tu_dir}", file=sys.stderr)
        result["tu_comparison"] = None

    # --- Active trip count from GTFS (optional) ---
    if args.gtfs:
        try:
            service_date = (
                args.date or vp_dir.parent.name
            )  # date is parent dir of vehiclepositions/
            active_trips = count_active_trips(args.gtfs, service_date)
            result["active_trips_from_gtfs"] = active_trips

            peak_vp: int = result.get("peak_vehicles_per_snapshot", 0)
            tu_comp = result.get("tu_comparison") or {}
            peak_tu: int = tu_comp.get("peak_trips_per_snapshot", 0)

            result["coverage_ratio"] = {
                "peak_vp_vehicles": peak_vp,
                "peak_tu_trips": peak_tu,
                "active_trips_static": active_trips,
                "vp_to_tu_ratio": round(peak_vp / max(peak_tu, 1), 3),
                "vp_to_active_ratio": round(peak_vp / max(active_trips, 1), 3),
                "note": (
                    "peak_vp = most VP entities in any single snapshot. "
                    "peak_tu = most TU entities in any single snapshot. "
                    "active_trips = trips from GTFS trips.txt active on service date."
                ),
            }
        except Exception as exc:
            print(f"WARNING: could not count active trips from GTFS: {exc}", file=sys.stderr)
            result["coverage_ratio"] = {"error": str(exc)}
    else:
        result["coverage_ratio"] = None

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
