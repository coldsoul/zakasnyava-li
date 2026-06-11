#!/usr/bin/env python3
"""Compute per-line reliability metrics for one calendar month.

Usage:
    python pipeline/compute_metrics.py --month YYYY-MM [--gtfs PATH]
"""

import argparse
import calendar
import csv
import io
import json
import os
import sqlite3
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SOFIA = ZoneInfo("Europe/Sofia")
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "data"))
DB_PATH = Path(os.environ.get("DB_PATH", "data/derived/stop_events.sqlite"))
SITE_DATA_DIR = Path(os.environ.get("SITE_DATA_DIR", "site/public/data"))

ONTIME_LO, ONTIME_HI = -60, 180  # on-time window in seconds
MIN_SAMPLE = 300  # minimum measured stop events per (direction, month)
HEATMAP_MIN_EVENTS = 20
FREQ_HEADWAY_SEC = 600  # ≤ 10 min peak headway → frequent-line caveat

# (label, lo_sec, hi_sec) — lo inclusive, hi exclusive
DIST_BUCKETS = [
    ("<-1m", float("-inf"), -60),
    ("-1..2m", -60, 120),
    ("2..5m", 120, 300),
    ("5..10m", 300, 600),
    ("10..15m", 600, 900),
    ("15+m", 900, float("inf")),
]


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def percentile_linear(sorted_vals: list, p: float) -> float:
    """Linear interpolation percentile over a sorted list."""
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    idx = p * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo])


def r1(x) -> float | None:
    return round(x, 1) if x is not None else None


def compute_score(served_pct: float, ontime_pct: float, p90_min: float) -> float:
    served = clamp01((served_pct - 90) / 10) * 100
    ontime = clamp01((ontime_pct - 50) / 45) * 100
    p90 = clamp01((15 - p90_min) / 12) * 100
    return 0.40 * served + 0.35 * ontime + 0.25 * p90


def score_to_grade(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "E"


def infer_type(route_id: str) -> str:
    if route_id.startswith("TM"):
        return "tram"
    if route_id.startswith("TB"):
        return "trolleybus"
    rid = route_id.upper()
    if rid.startswith("M") and rid[1:].isdigit():
        return "metro"
    return "bus"


# ---------------------------------------------------------------------------
# Metric builders
# ---------------------------------------------------------------------------


def build_distribution(delays: list) -> list:
    buckets = []
    for label, lo, hi in DIST_BUCKETS:
        count = sum(1 for d in delays if lo <= d < hi)
        buckets.append({"count": count, "label": label})
    return buckets


def build_heatmap(measured: list) -> list:
    """7 rows Mon..Sun × 19 cols hour 05..23. Value = median delay min; null if < 20 events."""
    cells: dict[tuple, list] = defaultdict(list)
    for e in measured:
        dt = datetime.fromtimestamp(e["scheduled_ts"], tz=SOFIA)
        h = dt.hour
        if 5 <= h <= 23:
            cells[(dt.weekday(), h - 5)].append(e["delay_sec"] / 60)
    hm = [[None] * 19 for _ in range(7)]
    for (dow, col), vals in cells.items():
        if len(vals) >= HEATMAP_MIN_EVENTS:
            hm[dow][col] = r1(percentile_linear(sorted(vals), 0.5))
    return hm


def build_weekly(measured: list, month_str: str) -> list:
    """ISO weeks overlapping the month, sorted ascending by week_start."""
    year, month = int(month_str[:4]), int(month_str[5:7])
    last_day = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)

    week_data: dict[date, list] = defaultdict(list)
    for e in measured:
        d = datetime.fromtimestamp(e["scheduled_ts"], tz=SOFIA).date()
        monday = d - timedelta(days=d.weekday())
        week_data[monday].append(e["delay_sec"] / 60)

    result = []
    for monday, vals in sorted(week_data.items()):
        if monday > month_end or (monday + timedelta(days=6)) < month_start:
            continue
        if len(vals) < 10:
            continue
        s = sorted(vals)
        result.append(
            {
                "median": r1(percentile_linear(s, 0.5)),
                "p90": r1(percentile_linear(s, 0.9)),
                "week_start": monday.isoformat(),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _query(db_path: Path, sql: str, params: tuple = ()) -> list:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def get_routes(db_path: Path, start: str, end: str) -> list:
    return [
        (r["route_id"], r["direction_id"])
        for r in _query(
            db_path,
            "SELECT DISTINCT route_id, direction_id FROM stop_events "
            "WHERE service_date BETWEEN ? AND ? ORDER BY route_id, direction_id",
            (start, end),
        )
    ]


def load_direction_events(
    db_path: Path, route_id: str, direction_id: int, start: str, end: str
) -> list:
    return _query(
        db_path,
        "SELECT trip_id, stop_sequence, scheduled_ts, delay_sec, is_ghost "
        "FROM stop_events "
        "WHERE route_id = ? AND direction_id = ? AND service_date BETWEEN ? AND ?",
        (route_id, direction_id, start, end),
    )


def is_frequent(db_path: Path, route_id: str, direction_id: int, start: str, end: str) -> bool:
    """True if median gap between consecutive trip departures (same day) ≤ FREQ_HEADWAY_SEC."""
    rows = _query(
        db_path,
        "SELECT service_date, MIN(scheduled_ts) AS first_ts "
        "FROM stop_events "
        "WHERE route_id = ? AND direction_id = ? AND service_date BETWEEN ? AND ? "
        "  AND is_ghost = 0 "
        "GROUP BY service_date, trip_id "
        "ORDER BY service_date, first_ts",
        (route_id, direction_id, start, end),
    )
    by_date: dict[str, list] = defaultdict(list)
    for r in rows:
        by_date[r["service_date"]].append(r["first_ts"])
    gaps = []
    for times in by_date.values():
        times.sort()
        gaps.extend(times[i + 1] - times[i] for i in range(len(times) - 1))
    if not gaps:
        return False
    gaps.sort()
    return percentile_linear(gaps, 0.5) <= FREQ_HEADWAY_SEC


# ---------------------------------------------------------------------------
# Direction stats
# ---------------------------------------------------------------------------


def compute_direction_stats(events: list, month_str: str) -> dict:
    measured = [e for e in events if not e["is_ghost"] and e["delay_sec"] is not None]
    all_trips = {e["trip_id"] for e in events}
    ghost_trips = {e["trip_id"] for e in events if e["is_ghost"]}
    n_trips = len(all_trips)
    served_pct = (n_trips - len(ghost_trips)) / n_trips * 100 if n_trips else 0.0

    delays = sorted(e["delay_sec"] for e in measured)
    n = len(delays)

    if n < MIN_SAMPLE:
        return {
            "distribution_buckets": build_distribution([]),
            "grade": "—",
            "heatmap": [[None] * 19 for _ in range(7)],
            "median": None,
            "ontime_pct": None,
            "p90": None,
            "sample_n": n,
            "score": None,
            "served_pct": r1(served_pct),
            "weekly": [],
        }

    median_s = percentile_linear(delays, 0.5)
    p90_s = percentile_linear(delays, 0.9)
    ontime = sum(1 for d in delays if ONTIME_LO <= d <= ONTIME_HI)
    ontime_pct = ontime / n * 100
    score = compute_score(served_pct, ontime_pct, p90_s / 60)

    return {
        "distribution_buckets": build_distribution(delays),
        "grade": score_to_grade(score),
        "heatmap": build_heatmap(measured),
        "median": r1(median_s / 60),
        "ontime_pct": r1(ontime_pct),
        "p90": r1(p90_s / 60),
        "sample_n": n,
        "score": r1(score),
        "served_pct": r1(served_pct),
        "weekly": build_weekly(measured, month_str),
    }


# ---------------------------------------------------------------------------
# Route metadata from GTFS
# ---------------------------------------------------------------------------


def load_route_meta(gtfs_zip: Path) -> dict:
    route_type_map = {0: "tram", 1: "metro", 3: "bus", 11: "trolleybus"}
    meta = {}
    with zipfile.ZipFile(gtfs_zip) as zf:
        with zf.open("routes.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
                rt = int(row.get("route_type") or 3)
                meta[row["route_id"]] = {
                    "name": row.get("route_short_name") or row["route_id"],
                    "type": route_type_map.get(rt, "bus"),
                }
    return meta


# ---------------------------------------------------------------------------
# Feed health
# ---------------------------------------------------------------------------


def build_feed_health(month_str: str, data_root: Path) -> dict:
    year, month = int(month_str[:4]), int(month_str[5:7])
    last_day = calendar.monthrange(year, month)[1]
    days = []
    for day in range(1, last_day + 1):
        date_str = f"{year}-{month:02d}-{day:02d}"
        snap_dir = data_root / "raw" / date_str / "tripupdates"
        if snap_dir.exists():
            ok = len(list(snap_dir.glob("*.pb.zst")))
            expected = 4320  # 24 h × 3600 / 20 s
            days.append(
                {
                    "date": date_str,
                    "pct": r1(ok / expected * 100),
                    "snapshots_expected": expected,
                    "snapshots_ok": ok,
                }
            )
    return {"days": days}


# ---------------------------------------------------------------------------
# Month helpers
# ---------------------------------------------------------------------------


def prev_month(month_str: str) -> str:
    year, month = int(month_str[:4]), int(month_str[5:7])
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


def month_bounds(month_str: str) -> tuple[str, str]:
    year, month = int(month_str[:4]), int(month_str[5:7])
    last = calendar.monthrange(year, month)[1]
    return f"{month_str}-01", f"{month_str}-{last:02d}"


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


# ---------------------------------------------------------------------------
# All-months helpers
# ---------------------------------------------------------------------------


def get_all_months(db_path: Path) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT DISTINCT strftime('%Y-%m', service_date) FROM stop_events ORDER BY 1"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def _find_gtfs(data_root: Path) -> "Path | None":
    candidates = sorted((data_root / "gtfs").glob("*.zip"), reverse=True)
    return candidates[0] if candidates else None


def compute_all(
    db_path: Path = DB_PATH,
    data_root: Path = DATA_ROOT,
    site_data_dir: Path = SITE_DATA_DIR,
    gtfs_zip: "Path | None" = None,
) -> dict:
    if gtfs_zip is None:
        gtfs_zip = _find_gtfs(data_root)
    months = get_all_months(db_path)
    by_month: dict[str, int] = {}
    for month_str in months:
        r = compute(month_str, db_path, data_root, site_data_dir, gtfs_zip)
        by_month[month_str] = r["lines_graded"]
    months_sorted = sorted(months, reverse=True)
    write_json(site_data_dir / "months.json", months_sorted)
    return {"months": months_sorted, "by_month": by_month}


# ---------------------------------------------------------------------------
# Main compute
# ---------------------------------------------------------------------------


def compute(
    month_str: str,
    db_path: Path = DB_PATH,
    data_root: Path = DATA_ROOT,
    site_data_dir: Path = SITE_DATA_DIR,
    gtfs_zip: Path | None = None,
) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    route_meta: dict[str, dict] = {}
    if gtfs_zip and gtfs_zip.exists():
        route_meta = load_route_meta(gtfs_zip)

    start, end = month_bounds(month_str)
    prev_ms = prev_month(month_str)
    prev_start, prev_end = month_bounds(prev_ms)

    by_route: dict[str, list] = defaultdict(list)
    for route_id, direction_id in get_routes(db_path, start, end):
        by_route[route_id].append(direction_id)

    index_rows = []

    for route_id in sorted(by_route):
        dir_ids = by_route[route_id]
        directions: list[tuple[int, dict]] = []
        for direction_id in dir_ids:
            evs = load_direction_events(db_path, route_id, direction_id, start, end)
            stats = compute_direction_stats(evs, month_str)
            directions.append((direction_id, stats))

        # Worse direction = lower score (higher lateness)
        scored = [(d, s) for d, s in directions if s["score"] is not None]
        _, worse = min(scored, key=lambda x: x[1]["score"]) if scored else directions[0]

        # Frequency caveat
        caveats = []
        for direction_id in dir_ids:
            if is_frequent(db_path, route_id, direction_id, start, end):
                caveats.append(
                    {
                        "text": "Честа линия — точността спрямо разписание е по-малко показателна.",
                        "type": "frequent_line",
                    }
                )
                break

        meta = route_meta.get(route_id, {})
        line_name = meta.get("name", route_id)
        line_type = meta.get("type", infer_type(route_id))

        write_json(
            site_data_dir / month_str / "line" / f"{route_id}.json",
            {
                "caveats": caveats,
                "distribution_buckets": worse["distribution_buckets"],
                "heatmap": worse["heatmap"],
                "meta": {"line_id": route_id, "name": line_name, "type": line_type},
                "per_direction": {str(d): s for d, s in directions},
                "weekly": worse["weekly"],
            },
        )

        if worse["grade"] == "—":
            continue

        # trend_mom: current worse-direction median minus previous month's
        trend: float | None = None
        if worse["median"] is not None:
            prev_medians = []
            for direction_id in dir_ids:
                pevs = load_direction_events(db_path, route_id, direction_id, prev_start, prev_end)
                pm = [e for e in pevs if not e["is_ghost"] and e["delay_sec"] is not None]
                if len(pm) >= MIN_SAMPLE:
                    pd_sorted = sorted(e["delay_sec"] for e in pm)
                    prev_medians.append(percentile_linear(pd_sorted, 0.5) / 60)
            if prev_medians:
                trend = r1(worse["median"] - max(prev_medians))  # max = worse prev direction

        index_rows.append(
            {
                "grade": worse["grade"],
                "line_id": route_id,
                "median": worse["median"],
                "name": line_name,
                "ontime_pct": worse["ontime_pct"],
                "p90": worse["p90"],
                "sample_n": worse["sample_n"],
                "score": worse["score"],
                "served_pct": worse["served_pct"],
                "trend_mom": trend,
                "type": line_type,
            }
        )

    index_rows.sort(key=lambda r: r["score"] or 0.0)

    write_json(site_data_dir / month_str / "index.json", index_rows)
    write_json(site_data_dir / month_str / "feed_health.json", build_feed_health(month_str, data_root))

    return {"lines_graded": len(index_rows)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--month", help="Month YYYY-MM")
    ap.add_argument("--all-months", action="store_true", help="Compute all months in DB")
    ap.add_argument("--gtfs", type=Path, help="GTFS zip path (auto-detected if omitted)")
    args = ap.parse_args()

    if not args.all_months and not args.month:
        ap.error("--month or --all-months required")

    if args.gtfs is None:
        args.gtfs = _find_gtfs(DATA_ROOT)

    if args.all_months:
        result = compute_all(DB_PATH, DATA_ROOT, SITE_DATA_DIR, args.gtfs)
    else:
        result = compute(args.month, gtfs_zip=args.gtfs)

    print(json.dumps(result))
    sys.exit(0)
