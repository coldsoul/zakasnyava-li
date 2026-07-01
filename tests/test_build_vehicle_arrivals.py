"""Unit tests for pipeline/build_vehicle_arrivals.py — 8 fixture cases."""

import io
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import zstandard as zstd
from google.transit import gtfs_realtime_pb2

import pipeline.build_vehicle_arrivals as bva

SOFIA = ZoneInfo("Europe/Sofia")
SERVICE_DATE = "2026-07-01"
DATE_COMPACT = "20260701"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_gtfs_zip(
    trips: list[dict],
    stop_times: list[dict],
    calendar_dates: list[dict],
    stops: list[dict] | None = None,
    routes: list[dict] | None = None,
) -> bytes:
    """Build minimal in-memory GTFS zip with stops.txt for coordinates."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:

        def csv_bytes(headers: list[str], rows: list[dict]) -> bytes:
            lines = [",".join(headers)]
            for row in rows:
                lines.append(",".join(str(row.get(h, "")) for h in headers))
            return "\n".join(lines).encode()

        routes = routes or [{"route_id": "R1", "route_short_name": "1", "route_type": "3"}]
        zf.writestr("routes.txt", csv_bytes(["route_id", "route_short_name", "route_type"], routes))
        zf.writestr(
            "trips.txt",
            csv_bytes(["trip_id", "route_id", "service_id", "direction_id"], trips),
        )
        zf.writestr(
            "stop_times.txt",
            csv_bytes(
                ["trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"],
                stop_times,
            ),
        )
        zf.writestr(
            "calendar_dates.txt",
            csv_bytes(["service_id", "date", "exception_type"], calendar_dates),
        )
        if stops:
            zf.writestr(
                "stops.txt",
                csv_bytes(["stop_id", "stop_lat", "stop_lon", "stop_name"], stops),
            )
    return buf.getvalue()


def make_vp_snapshot(entities: list[dict], header_ts: int) -> bytes:
    """Build FeedMessage protobuf bytes with VehiclePosition entities.

    Each entity dict: {trip_id, stop_id, vehicle_id, lat, lon, measured_ts,
                        speed=None, bearing=None}
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = header_ts
    for ent in entities:
        entity = feed.entity.add()
        entity.id = ent.get("trip_id", "unknown")
        vp = entity.vehicle
        vp.trip.trip_id = ent["trip_id"]
        vp.trip.schedule_relationship = ent.get("schedule_relationship", 0)
        vp.stop_id = ent["stop_id"]
        vp.vehicle.id = ent["vehicle_id"]
        vp.position.latitude = ent["lat"]
        vp.position.longitude = ent["lon"]
        if "speed" in ent and ent["speed"] is not None:
            vp.position.speed = ent["speed"]
        if "bearing" in ent and ent["bearing"] is not None:
            vp.position.bearing = ent["bearing"]
        vp.timestamp = ent["measured_ts"]
    return feed.SerializeToString()


def write_vp_snapshot(directory: Path, header_ts: int, pb_bytes: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    dt = datetime.utcfromtimestamp(header_ts)
    name = dt.strftime("%H%M%S") + ".pb.zst"
    cctx = zstd.ZstdCompressor(level=1)
    (directory / name).write_bytes(cctx.compress(pb_bytes))


def sofia_epoch(date_str: str, hhmm: str) -> int:
    """Return Unix epoch for date + HH:MM in Europe/Sofia."""
    from datetime import timedelta

    h, m = (int(x) for x in hhmm.split(":"))
    base = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=SOFIA)
    return int((base + timedelta(hours=h, minutes=m)).timestamp())


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


def run_vp_match(
    tmp_path: Path,
    gtfs_bytes: bytes,
    snapshots: list[tuple[int, list[dict]]],
) -> list[dict]:
    """Write GTFS zip + VP snapshot files, return match_vp() result."""
    gtfs_path = tmp_path / "gtfs.zip"
    gtfs_path.write_bytes(gtfs_bytes)
    snap_dir = tmp_path / "snapshots"
    for hts, entities in snapshots:
        write_vp_snapshot(snap_dir, hts, make_vp_snapshot(entities, hts))

    from pipeline.build_stop_events import load_gtfs, load_stop_coordinates

    active_trips, schedule_map, trip_all_stops, _ = load_gtfs(gtfs_path, SERVICE_DATE)
    stop_coords = load_stop_coordinates(gtfs_path)
    return bva.match_vp(
        SERVICE_DATE,
        active_trips,
        schedule_map,
        trip_all_stops,
        stop_coords,
        snap_dir,
    )


# ---------------------------------------------------------------------------
# Case 1: Normal arrival — VP entity matches schedule, delay computed
# ---------------------------------------------------------------------------


def test_normal_vp_arrival(tmp_path):
    """Bus reports position at stop A1 at 09:02, scheduled at 09:00 → delay=120s."""
    sched_ts = sofia_epoch(SERVICE_DATE, "09:00")
    measured_ts = sched_ts + 120  # 2 min late

    gtfs = make_gtfs_zip(
        trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "S1", "direction_id": "0"}],
        stop_times=[
            {
                "trip_id": "T1",
                "stop_id": "A1",
                "stop_sequence": "1",
                "arrival_time": "09:00:00",
                "departure_time": "09:00:00",
            }
        ],
        calendar_dates=[{"service_id": "S1", "date": DATE_COMPACT, "exception_type": "1"}],
        stops=[
            {"stop_id": "A1", "stop_lat": "42.6952", "stop_lon": "23.3218", "stop_name": "Stop A1"}
        ],
    )

    rows = run_vp_match(
        tmp_path,
        gtfs,
        [
            (
                sched_ts - 60,
                [
                    {
                        "trip_id": "T1",
                        "stop_id": "A1",
                        "vehicle_id": "V42A",
                        "lat": 42.6952,
                        "lon": 23.3218,
                        "measured_ts": measured_ts,
                    }
                ],
            )
        ],
    )

    assert len(rows) == 1
    assert rows[0]["delay_sec"] == 120
    assert rows[0]["actual_arrival"] == measured_ts
    assert rows[0]["trip_id"] == "T1"
    assert rows[0]["stop_id"] == "A1"
    assert rows[0]["vehicle_id"] == "V42A"
    assert rows[0]["source"] == "vp"
    assert rows[0]["gps_distance_m"] is not None and rows[0]["gps_distance_m"] >= 0


# ---------------------------------------------------------------------------
# Case 2: No VP coverage → no rows produced (TU fallback handled by merge, not here)
# ---------------------------------------------------------------------------


def test_no_vp_coverage_empty_rows(tmp_path):
    """Trip is active but has zero VP snapshots → no vehicle_arrivals rows."""
    sched_ts = sofia_epoch(SERVICE_DATE, "09:00")

    gtfs = make_gtfs_zip(
        trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "S1", "direction_id": "0"}],
        stop_times=[
            {
                "trip_id": "T1",
                "stop_id": "A1",
                "stop_sequence": "1",
                "arrival_time": "09:00:00",
                "departure_time": "09:00:00",
            }
        ],
        calendar_dates=[{"service_id": "S1", "date": DATE_COMPACT, "exception_type": "1"}],
        stops=[
            {"stop_id": "A1", "stop_lat": "42.6952", "stop_lon": "23.3218", "stop_name": "Stop A1"}
        ],
    )

    # Empty snapshot (no entities at all)
    rows = run_vp_match(tmp_path, gtfs, [(sched_ts, [])])

    assert len(rows) == 0  # trip has no VP arrival data


# ---------------------------------------------------------------------------
# Case 3: VP disproves ghost — VP reports arrival where no TU prediction exists
# ---------------------------------------------------------------------------


def test_vp_disproves_ghost(tmp_path):
    """Trip has no TripUpdates but VP reports it at its stops → it was NOT a ghost."""
    sched_ts = sofia_epoch(SERVICE_DATE, "09:00")
    measured_ts = sched_ts + 60  # 1 min late

    gtfs = make_gtfs_zip(
        trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "S1", "direction_id": "0"}],
        stop_times=[
            {
                "trip_id": "T1",
                "stop_id": "A1",
                "stop_sequence": "1",
                "arrival_time": "09:00:00",
                "departure_time": "09:00:00",
            }
        ],
        calendar_dates=[{"service_id": "S1", "date": DATE_COMPACT, "exception_type": "1"}],
        stops=[
            {"stop_id": "A1", "stop_lat": "42.6952", "stop_lon": "23.3218", "stop_name": "Stop A1"}
        ],
    )

    rows = run_vp_match(
        tmp_path,
        gtfs,
        [
            (
                sched_ts - 60,
                [
                    {
                        "trip_id": "T1",
                        "stop_id": "A1",
                        "vehicle_id": "V42A",
                        "lat": 42.6952,
                        "lon": 23.3218,
                        "measured_ts": measured_ts,
                    }
                ],
            )
        ],
    )

    assert len(rows) == 1
    assert rows[0]["delay_sec"] == 60
    assert rows[0]["source"] == "vp"
    # Trip T1 was never seen by TU but VP says it arrived — disproving the ghost


# ---------------------------------------------------------------------------
# Case 4: Very late arrival — real GPS delay beyond what TU captures
# ---------------------------------------------------------------------------


def test_very_late_vp_arrival(tmp_path):
    """Bus arrives 25 min late — GPS still tracks it after TU drops off."""
    sched_ts = sofia_epoch(SERVICE_DATE, "09:00")
    measured_ts = sched_ts + 1500  # 25 min late

    gtfs = make_gtfs_zip(
        trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "S1", "direction_id": "0"}],
        stop_times=[
            {
                "trip_id": "T1",
                "stop_id": "A1",
                "stop_sequence": "1",
                "arrival_time": "09:00:00",
                "departure_time": "09:00:00",
            }
        ],
        calendar_dates=[{"service_id": "S1", "date": DATE_COMPACT, "exception_type": "1"}],
        stops=[
            {"stop_id": "A1", "stop_lat": "42.6952", "stop_lon": "23.3218", "stop_name": "Stop A1"}
        ],
    )

    rows = run_vp_match(
        tmp_path,
        gtfs,
        [
            (
                measured_ts - 20,
                [
                    {
                        "trip_id": "T1",
                        "stop_id": "A1",
                        "vehicle_id": "V42A",
                        "lat": 42.6952,
                        "lon": 23.3218,
                        "measured_ts": measured_ts,
                    }
                ],
            )
        ],
    )

    assert len(rows) == 1
    assert rows[0]["delay_sec"] == 1500
    assert rows[0]["source"] == "vp"


# ---------------------------------------------------------------------------
# Case 5: GPS validation flag — vehicle reports stop but GPS is far away
# ---------------------------------------------------------------------------


def test_gps_far_from_stop_flagged(tmp_path):
    """Vehicle reports stop A1 but GPS shows it 500m away → distance recorded, not rejected."""
    sched_ts = sofia_epoch(SERVICE_DATE, "09:00")
    measured_ts = sched_ts + 120

    # Stop A1 at (42.6952, 23.3218). Vehicle reports at (42.7000, 23.3250) ~500m away.
    gtfs = make_gtfs_zip(
        trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "S1", "direction_id": "0"}],
        stop_times=[
            {
                "trip_id": "T1",
                "stop_id": "A1",
                "stop_sequence": "1",
                "arrival_time": "09:00:00",
                "departure_time": "09:00:00",
            }
        ],
        calendar_dates=[{"service_id": "S1", "date": DATE_COMPACT, "exception_type": "1"}],
        stops=[
            {"stop_id": "A1", "stop_lat": "42.6952", "stop_lon": "23.3218", "stop_name": "Stop A1"}
        ],
    )

    rows = run_vp_match(
        tmp_path,
        gtfs,
        [
            (
                sched_ts - 60,
                [
                    {
                        "trip_id": "T1",
                        "stop_id": "A1",
                        "vehicle_id": "V42A",
                        "lat": 42.7000,
                        "lon": 23.3250,
                        "measured_ts": measured_ts,
                    }
                ],
            )
        ],
    )

    assert len(rows) == 1
    # Still produces a row — GPS mismatch doesn't reject, only flags
    assert rows[0]["delay_sec"] == 120
    assert rows[0]["gps_distance_m"] > 150  # flagged as implausible


# ---------------------------------------------------------------------------
# Case 6: Noise discarded — |delay| > 3 hours
# ---------------------------------------------------------------------------


def test_noise_discarded(tmp_path):
    """Delay of 4 hours → row discarded as feed noise."""
    sched_ts = sofia_epoch(SERVICE_DATE, "09:00")
    measured_ts = sched_ts + 4 * 3600  # 4 h

    gtfs = make_gtfs_zip(
        trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "S1", "direction_id": "0"}],
        stop_times=[
            {
                "trip_id": "T1",
                "stop_id": "A1",
                "stop_sequence": "1",
                "arrival_time": "09:00:00",
                "departure_time": "09:00:00",
            }
        ],
        calendar_dates=[{"service_id": "S1", "date": DATE_COMPACT, "exception_type": "1"}],
        stops=[
            {"stop_id": "A1", "stop_lat": "42.6952", "stop_lon": "23.3218", "stop_name": "Stop A1"}
        ],
    )

    rows = run_vp_match(
        tmp_path,
        gtfs,
        [
            (
                measured_ts - 20,
                [
                    {
                        "trip_id": "T1",
                        "stop_id": "A1",
                        "vehicle_id": "V42A",
                        "lat": 42.6952,
                        "lon": 23.3218,
                        "measured_ts": measured_ts,
                    }
                ],
            )
        ],
    )

    assert len(rows) == 0  # discarded as noise


# ---------------------------------------------------------------------------
# Case 7: Multiple VP snapshots — keep the latest measurement per stop
# ---------------------------------------------------------------------------


def test_keeps_latest_measurement(tmp_path):
    """Two VP snapshots for same (trip, stop): earlier one discarded, later one wins."""
    sched_ts = sofia_epoch(SERVICE_DATE, "09:00")
    early_ts = sched_ts + 60
    late_ts = sched_ts + 90

    gtfs = make_gtfs_zip(
        trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "S1", "direction_id": "0"}],
        stop_times=[
            {
                "trip_id": "T1",
                "stop_id": "A1",
                "stop_sequence": "1",
                "arrival_time": "09:00:00",
                "departure_time": "09:00:00",
            }
        ],
        calendar_dates=[{"service_id": "S1", "date": DATE_COMPACT, "exception_type": "1"}],
        stops=[
            {"stop_id": "A1", "stop_lat": "42.6952", "stop_lon": "23.3218", "stop_name": "Stop A1"}
        ],
    )

    rows = run_vp_match(
        tmp_path,
        gtfs,
        [
            # Earlier snapshot
            (
                early_ts - 10,
                [
                    {
                        "trip_id": "T1",
                        "stop_id": "A1",
                        "vehicle_id": "V42A",
                        "lat": 42.6952,
                        "lon": 23.3218,
                        "measured_ts": early_ts,
                    }
                ],
            ),
            # Later snapshot — should win
            (
                late_ts - 10,
                [
                    {
                        "trip_id": "T1",
                        "stop_id": "A1",
                        "vehicle_id": "V42A",
                        "lat": 42.6952,
                        "lon": 23.3218,
                        "measured_ts": late_ts,
                    }
                ],
            ),
        ],
    )

    assert len(rows) == 1
    assert rows[0]["actual_arrival"] == late_ts  # later measurement
    assert rows[0]["delay_sec"] == late_ts - sched_ts  # = 90


# ---------------------------------------------------------------------------
# Case 8: Multi-stop trip — each stop gets its own row
# ---------------------------------------------------------------------------


def test_multi_stop_trip_each_stop_row(tmp_path):
    """Trip with 3 stops → 3 rows in vehicle_arrivals."""
    s1_ts = sofia_epoch(SERVICE_DATE, "09:00")
    s2_ts = sofia_epoch(SERVICE_DATE, "09:05")
    s3_ts = sofia_epoch(SERVICE_DATE, "09:10")

    m1 = s1_ts + 120  # +2 min
    m2 = s2_ts + 60  # +1 min
    m3 = s3_ts + 300  # +5 min — late!

    gtfs = make_gtfs_zip(
        trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "S1", "direction_id": "0"}],
        stop_times=[
            {
                "trip_id": "T1",
                "stop_id": "A1",
                "stop_sequence": "1",
                "arrival_time": "09:00:00",
                "departure_time": "09:00:00",
            },
            {
                "trip_id": "T1",
                "stop_id": "A2",
                "stop_sequence": "2",
                "arrival_time": "09:05:00",
                "departure_time": "09:05:00",
            },
            {
                "trip_id": "T1",
                "stop_id": "A3",
                "stop_sequence": "3",
                "arrival_time": "09:10:00",
                "departure_time": "09:10:00",
            },
        ],
        calendar_dates=[{"service_id": "S1", "date": DATE_COMPACT, "exception_type": "1"}],
        stops=[
            {"stop_id": "A1", "stop_lat": "42.6952", "stop_lon": "23.3218", "stop_name": "Stop A1"},
            {"stop_id": "A2", "stop_lat": "42.6960", "stop_lon": "23.3225", "stop_name": "Stop A2"},
            {"stop_id": "A3", "stop_lat": "42.6970", "stop_lon": "23.3230", "stop_name": "Stop A3"},
        ],
    )

    rows = run_vp_match(
        tmp_path,
        gtfs,
        [
            (
                m3,
                [
                    {
                        "trip_id": "T1",
                        "stop_id": "A1",
                        "vehicle_id": "V42A",
                        "lat": 42.6952,
                        "lon": 23.3218,
                        "measured_ts": m1,
                    },
                    {
                        "trip_id": "T1",
                        "stop_id": "A2",
                        "vehicle_id": "V42A",
                        "lat": 42.6960,
                        "lon": 23.3225,
                        "measured_ts": m2,
                    },
                    {
                        "trip_id": "T1",
                        "stop_id": "A3",
                        "vehicle_id": "V42A",
                        "lat": 42.6970,
                        "lon": 23.3230,
                        "measured_ts": m3,
                    },
                ],
            )
        ],
    )

    assert len(rows) == 3
    assert rows[0]["stop_sequence"] == 1 and rows[0]["delay_sec"] == 120
    assert rows[1]["stop_sequence"] == 2 and rows[1]["delay_sec"] == 60
    assert rows[2]["stop_sequence"] == 3 and rows[2]["delay_sec"] == 300
