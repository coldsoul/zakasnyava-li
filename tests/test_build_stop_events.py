"""Unit tests for pipeline/build_stop_events.py — all 8 SPEC.md §7 Story 2 fixture cases."""

import io
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import zstandard as zstd
from google.transit import gtfs_realtime_pb2

import pipeline.build_stop_events as bse

SOFIA = ZoneInfo("Europe/Sofia")
SERVICE_DATE = "2026-06-11"
DATE_COMPACT = "20260611"

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_gtfs_zip(
    trips: list[dict],
    stop_times: list[dict],
    calendar_dates: list[dict],
    routes: list[dict] | None = None,
) -> bytes:
    """Build minimal in-memory GTFS zip."""
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
            "trips.txt", csv_bytes(["trip_id", "route_id", "service_id", "direction_id"], trips)
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
    return buf.getvalue()


def make_snapshot(entities: list[dict], header_ts: int) -> bytes:
    """Build FeedMessage protobuf bytes.

    Each entity dict: {trip_id, schedule_relationship=0,
                       stops: [{stop_id, predicted_arrival, stop_sched_rel=0}]}
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = header_ts
    for ent in entities:
        entity = feed.entity.add()
        entity.id = ent["trip_id"]
        tu = entity.trip_update
        tu.trip.trip_id = ent["trip_id"]
        tu.trip.schedule_relationship = ent.get("schedule_relationship", 0)
        for stop in ent.get("stops", []):
            stu = tu.stop_time_update.add()
            stu.stop_id = stop["stop_id"]
            stu.schedule_relationship = stop.get("stop_sched_rel", 0)
            if "predicted_arrival" in stop:
                stu.arrival.time = stop["predicted_arrival"]
            if "predicted_departure" in stop:
                stu.departure.time = stop["predicted_departure"]
    return feed.SerializeToString()


def write_snapshot_file(directory: Path, header_ts: int, pb_bytes: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    dt = datetime.utcfromtimestamp(header_ts)
    name = dt.strftime("%H%M%S") + ".pb.zst"
    cctx = zstd.ZstdCompressor(level=1)
    (directory / name).write_bytes(cctx.compress(pb_bytes))


def sofia_epoch(date_str: str, hhmm: str) -> int:
    """Return Unix epoch for a date + HH:MM in Europe/Sofia."""
    from datetime import timedelta

    h, m = (int(x) for x in hhmm.split(":"))
    base = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=SOFIA)
    return int((base + timedelta(hours=h, minutes=m)).timestamp())


# ---------------------------------------------------------------------------
# Helpers for running match() in isolation
# ---------------------------------------------------------------------------


def run_match(
    tmp_path: Path, gtfs_bytes: bytes, snapshots: list[tuple[int, list[dict]]]
) -> list[dict]:
    """Write GTFS zip + snapshot files, return match() result."""
    gtfs_path = tmp_path / "gtfs.zip"
    gtfs_path.write_bytes(gtfs_bytes)
    snap_dir = tmp_path / "snapshots"
    for hts, entities in snapshots:
        write_snapshot_file(snap_dir, hts, make_snapshot(entities, hts))

    active_trips, schedule_map, trip_all_stops, trip_first_dep = bse.load_gtfs(
        gtfs_path, SERVICE_DATE
    )
    return bse.match(
        SERVICE_DATE, active_trips, schedule_map, trip_all_stops, trip_first_dep, snap_dir
    )


# ---------------------------------------------------------------------------
# Case 1: Normal trip
# ---------------------------------------------------------------------------


def test_normal_trip_delay_computed(tmp_path):
    sched_ts = sofia_epoch(SERVICE_DATE, "09:00")
    predicted_ts = sched_ts + 120  # 2 min late

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
    )
    rows = run_match(
        tmp_path,
        gtfs,
        [
            (
                sched_ts - 60,
                [
                    {
                        "trip_id": "T1",
                        "stops": [{"stop_id": "A1", "predicted_arrival": predicted_ts}],
                    },
                ],
            )
        ],
    )

    assert len(rows) == 1
    assert rows[0]["delay_sec"] == 120
    assert rows[0]["is_ghost"] == 0
    assert rows[0]["trip_id"] == "T1"


# ---------------------------------------------------------------------------
# Case 2: Ghost trip — active but never observed
# ---------------------------------------------------------------------------


def test_ghost_trip_no_predictions(tmp_path):
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
    )
    # Empty snapshot file — no entities
    rows = run_match(tmp_path, gtfs, [(sched_ts, [])])

    assert len(rows) == 1
    assert rows[0]["is_ghost"] == 1
    assert rows[0]["delay_sec"] is None


# ---------------------------------------------------------------------------
# Case 3: Canceled trip (schedule_relationship=3) → ghost
# ---------------------------------------------------------------------------


def test_canceled_trip_is_ghost(tmp_path):
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
    )
    rows = run_match(
        tmp_path,
        gtfs,
        [
            (
                sched_ts - 60,
                [
                    {"trip_id": "T1", "schedule_relationship": 3, "stops": []},
                ],
            )
        ],
    )

    assert len(rows) == 1
    assert rows[0]["is_ghost"] == 1
    assert rows[0]["delay_sec"] is None


# ---------------------------------------------------------------------------
# Case 4: Early running — negative delay preserved (not floored at 0)
# ---------------------------------------------------------------------------


def test_early_trip_negative_delay(tmp_path):
    sched_ts = sofia_epoch(SERVICE_DATE, "09:00")
    predicted_ts = sched_ts - 90  # 90 s early

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
    )
    rows = run_match(
        tmp_path,
        gtfs,
        [
            (
                sched_ts - 120,
                [
                    {
                        "trip_id": "T1",
                        "stops": [{"stop_id": "A1", "predicted_arrival": predicted_ts}],
                    },
                ],
            )
        ],
    )

    assert len(rows) == 1
    assert rows[0]["delay_sec"] == -90
    assert rows[0]["is_ghost"] == 0


# ---------------------------------------------------------------------------
# Case 5: Midnight span — GTFS time "25:30:00" belongs to next calendar day
# ---------------------------------------------------------------------------


def test_midnight_span_gtfs_time_over_24h(tmp_path):
    # "25:30:00" for service date 2026-06-11 = 2026-06-12 01:30 Sofia
    expected_epoch = bse.gtfs_time_to_epoch(SERVICE_DATE, "25:30:00")
    predicted_ts = expected_epoch + 60  # 1 min late

    gtfs = make_gtfs_zip(
        trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "S1", "direction_id": "0"}],
        stop_times=[
            {
                "trip_id": "T1",
                "stop_id": "A1",
                "stop_sequence": "1",
                "arrival_time": "25:30:00",
                "departure_time": "25:30:00",
            }
        ],
        calendar_dates=[{"service_id": "S1", "date": DATE_COMPACT, "exception_type": "1"}],
    )
    # Snapshot taken well before the 25:30 stop but within 30-min window
    snap_hts = expected_epoch - 100
    rows = run_match(
        tmp_path,
        gtfs,
        [
            (
                snap_hts,
                [
                    {
                        "trip_id": "T1",
                        "stops": [{"stop_id": "A1", "predicted_arrival": predicted_ts}],
                    },
                ],
            )
        ],
    )

    assert len(rows) == 1
    assert rows[0]["delay_sec"] == 60
    assert rows[0]["scheduled_ts"] == expected_epoch


# ---------------------------------------------------------------------------
# Case 6: Loop route — trip visits same stop twice; each prediction → correct seq
# ---------------------------------------------------------------------------


def test_loop_route_disambiguates_by_nearest_scheduled_time(tmp_path):
    dep1 = sofia_epoch(SERVICE_DATE, "07:00")
    dep2 = sofia_epoch(SERVICE_DATE, "08:30")
    pred1 = dep1 + 60  # 1 min late at seq=1
    pred2 = dep2 + 120  # 2 min late at seq=5

    gtfs = make_gtfs_zip(
        trips=[{"trip_id": "LOOP", "route_id": "R1", "service_id": "S1", "direction_id": "0"}],
        stop_times=[
            {
                "trip_id": "LOOP",
                "stop_id": "S1",
                "stop_sequence": "1",
                "arrival_time": "07:00:00",
                "departure_time": "07:00:00",
            },
            {
                "trip_id": "LOOP",
                "stop_id": "S1",
                "stop_sequence": "5",
                "arrival_time": "08:30:00",
                "departure_time": "08:30:00",
            },
        ],
        calendar_dates=[{"service_id": "S1", "date": DATE_COMPACT, "exception_type": "1"}],
    )
    # Two snapshots, each with a prediction for S1 at different predicted times
    rows = run_match(
        tmp_path,
        gtfs,
        [
            (
                dep1 - 60,
                [{"trip_id": "LOOP", "stops": [{"stop_id": "S1", "predicted_arrival": pred1}]}],
            ),
            (
                dep2 - 60,
                [{"trip_id": "LOOP", "stops": [{"stop_id": "S1", "predicted_arrival": pred2}]}],
            ),
        ],
    )

    by_seq = {r["stop_sequence"]: r for r in rows}
    assert by_seq[1]["delay_sec"] == 60
    assert by_seq[5]["delay_sec"] == 120


# ---------------------------------------------------------------------------
# Case 7: Trip with inactive service — must NOT appear in output at all
# ---------------------------------------------------------------------------


def test_inactive_service_trip_excluded(tmp_path):
    sched_ts = sofia_epoch(SERVICE_DATE, "09:00")

    gtfs = make_gtfs_zip(
        trips=[
            {"trip_id": "ACTIVE", "route_id": "R1", "service_id": "S1", "direction_id": "0"},
            {
                "trip_id": "INACTIVE",
                "route_id": "R1",
                "service_id": "S_INACTIVE",
                "direction_id": "0",
            },
        ],
        stop_times=[
            {
                "trip_id": "ACTIVE",
                "stop_id": "A1",
                "stop_sequence": "1",
                "arrival_time": "09:00:00",
                "departure_time": "09:00:00",
            },
            {
                "trip_id": "INACTIVE",
                "stop_id": "A1",
                "stop_sequence": "1",
                "arrival_time": "09:00:00",
                "departure_time": "09:00:00",
            },
        ],
        calendar_dates=[
            {"service_id": "S1", "date": DATE_COMPACT, "exception_type": "1"},
            # S_INACTIVE has no entry for this date → not active
        ],
    )
    rows = run_match(
        tmp_path,
        gtfs,
        [
            (
                sched_ts - 60,
                [
                    {
                        "trip_id": "INACTIVE",
                        "stops": [{"stop_id": "A1", "predicted_arrival": sched_ts}],
                    },
                ],
            )
        ],
    )

    trip_ids = {r["trip_id"] for r in rows}
    assert "INACTIVE" not in trip_ids


# ---------------------------------------------------------------------------
# Case 8: Truncated/corrupt snapshot file — log and skip, do not abort
# ---------------------------------------------------------------------------


def test_truncated_snapshot_skipped_not_aborted(tmp_path):
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
    )
    gtfs_path = tmp_path / "gtfs.zip"
    gtfs_path.write_bytes(gtfs)
    snap_dir = tmp_path / "snapshots"

    # Good snapshot
    good_hts = sched_ts - 60
    predicted_ts = sched_ts + 180
    write_snapshot_file(
        snap_dir,
        good_hts,
        make_snapshot(
            [{"trip_id": "T1", "stops": [{"stop_id": "A1", "predicted_arrival": predicted_ts}]}],
            good_hts,
        ),
    )
    # Corrupt file (truncated zstd garbage) — lexicographically BEFORE good file
    (snap_dir / "000000.pb.zst").write_bytes(b"not zstd data at all \x00\xff")

    active_trips, schedule_map, trip_all_stops, trip_first_dep = bse.load_gtfs(
        gtfs_path, SERVICE_DATE
    )
    rows = bse.match(
        SERVICE_DATE, active_trips, schedule_map, trip_all_stops, trip_first_dep, snap_dir
    )

    # The corrupt file is skipped; the good snapshot still produces a row
    assert len(rows) == 1
    assert rows[0]["delay_sec"] == 180


# ---------------------------------------------------------------------------
# Idempotency: re-running replaces the date's rows
# ---------------------------------------------------------------------------


def test_write_to_db_is_idempotent(tmp_path):
    db = tmp_path / "db" / "stop_events.sqlite"

    rows1 = [
        {
            "service_date": SERVICE_DATE,
            "route_id": "R1",
            "direction_id": 0,
            "trip_id": "T1",
            "stop_id": "A1",
            "stop_sequence": 1,
            "scheduled_ts": 1000,
            "delay_sec": 60,
            "is_ghost": 0,
        }
    ]
    rows2 = [
        {
            "service_date": SERVICE_DATE,
            "route_id": "R1",
            "direction_id": 0,
            "trip_id": "T1",
            "stop_id": "A1",
            "stop_sequence": 1,
            "scheduled_ts": 1000,
            "delay_sec": 90,
            "is_ghost": 0,
        }
    ]  # updated delay

    bse.write_to_db(db, SERVICE_DATE, rows1)
    bse.write_to_db(db, SERVICE_DATE, rows2)  # second run replaces

    con = sqlite3.connect(db)
    count = con.execute("SELECT COUNT(*) FROM stop_events").fetchone()[0]
    delay = con.execute("SELECT delay_sec FROM stop_events").fetchone()[0]
    con.close()

    assert count == 1
    assert delay == 90  # second run's value, not first's


# ---------------------------------------------------------------------------
# gtfs_time_to_epoch boundary
# ---------------------------------------------------------------------------


def test_gtfs_time_to_epoch_normal():
    # 09:00:00 on 2026-06-11 Sofia
    ts = bse.gtfs_time_to_epoch("2026-06-11", "09:00:00")
    dt = datetime.fromtimestamp(ts, tz=SOFIA)
    assert dt.hour == 9 and dt.minute == 0


def test_gtfs_time_to_epoch_over_24h():
    # "25:30:00" = 01:30 next calendar day in Sofia timezone
    ts = bse.gtfs_time_to_epoch("2026-06-11", "25:30:00")
    dt = datetime.fromtimestamp(ts, tz=SOFIA)
    assert dt.day == 12 and dt.hour == 1 and dt.minute == 30
