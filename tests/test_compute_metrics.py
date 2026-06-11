"""Tests for pipeline/compute_metrics.py."""

import json
import sqlite3
from pathlib import Path

from pipeline.compute_metrics import (
    clamp01,
    compute,
    compute_all,
    compute_score,
    percentile_linear,
    r1,
    score_to_grade,
)

SCHEMA_SQL = (Path(__file__).parent.parent / "pipeline" / "schemas.sql").read_text()
GOLDEN_DIR = Path(__file__).parent / "golden"

# 2026-06-{01..08} 09:00 EEST = UTC+3  (Sofia summer time)
_BASE_TS = {
    "2026-06-01": 1780293600,
    "2026-06-02": 1780380000,
    "2026-06-03": 1780466400,
    "2026-06-04": 1780552800,
    "2026-06-05": 1780639200,
    "2026-06-06": 1780725600,
    "2026-06-07": 1780812000,
    "2026-06-08": 1780898400,
}


def _make_fixture_db(db_path: Path) -> None:
    """Create golden fixture: route R1 dir 0, 8 trips × 38 stops = 304 measured events.

    T1-T4 (Jun 1-4, Mon-Thu): delay = 60 s.
    T5-T8 (Jun 5-8, Fri-Mon): delay = 300 s.
    """
    con = sqlite3.connect(str(db_path))
    with con:
        con.executescript(SCHEMA_SQL)
        rows = []
        for i, (date_str, base) in enumerate(_BASE_TS.items()):
            delay = 60 if i < 4 else 300
            for j in range(1, 39):  # 38 stops
                rows.append((date_str, "R1", 0, f"T{i + 1}", f"S{j}", j, base + j * 60, delay, 0))
        con.executemany(
            "INSERT INTO stop_events "
            "(service_date, route_id, direction_id, trip_id, stop_id, "
            " stop_sequence, scheduled_ts, delay_sec, is_ghost) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    con.close()


# ---------------------------------------------------------------------------
# Unit tests — math helpers
# ---------------------------------------------------------------------------


def test_grade_boundary_a():
    assert score_to_grade(85.0) == "A"


def test_grade_boundary_b():
    assert score_to_grade(84.9) == "B"


def test_grade_thresholds():
    assert score_to_grade(100.0) == "A"
    assert score_to_grade(85.0) == "A"
    assert score_to_grade(70.0) == "B"
    assert score_to_grade(69.9) == "C"
    assert score_to_grade(55.0) == "C"
    assert score_to_grade(54.9) == "D"
    assert score_to_grade(40.0) == "D"
    assert score_to_grade(39.9) == "E"
    assert score_to_grade(0.0) == "E"


def test_score_perfect():
    # served=100, ontime=95, p90=3 min → all components = 100 → score = 100
    assert abs(compute_score(100, 95, 3) - 100.0) < 1e-9


def test_score_worst():
    # served=90, ontime=50, p90=15 min → all components = 0 → score = 0
    assert abs(compute_score(90, 50, 15) - 0.0) < 1e-9


def test_clamp01():
    assert clamp01(-1.0) == 0.0
    assert clamp01(0.5) == 0.5
    assert clamp01(2.0) == 1.0


def test_percentile_linear_exact():
    vals = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert percentile_linear(vals, 0.0) == 0.0
    assert percentile_linear(vals, 1.0) == 4.0
    assert percentile_linear(vals, 0.5) == 2.0  # exact midpoint


def test_percentile_linear_interpolation():
    vals = [0.0, 10.0]
    # p=0.5 → idx=0.5 → 0 + 0.5*(10-0) = 5.0
    assert abs(percentile_linear(vals, 0.5) - 5.0) < 1e-9
    # p=0.25 → idx=0.25 → 2.5
    assert abs(percentile_linear(vals, 0.25) - 2.5) < 1e-9


def test_r1_none():
    assert r1(None) is None


def test_r1_rounds():
    assert r1(3.833333) == 3.8
    assert r1(3.85) == 3.9  # banker's or half-up depending on Python; just check it's 1 dp
    assert len(str(r1(60.8333)).split(".")[-1]) == 1


# ---------------------------------------------------------------------------
# Golden-file integration test
# ---------------------------------------------------------------------------


def test_golden_output(tmp_path):
    db = tmp_path / "stop_events.sqlite"
    site = tmp_path / "data"
    _make_fixture_db(db)

    result = compute(
        month_str="2026-06",
        db_path=db,
        data_root=tmp_path / "rawdata",
        site_data_dir=site,
    )
    assert result["lines_graded"] == 1

    # Byte-identical comparison against committed golden files
    for rel in ("index.json", "line/R1.json", "feed_health.json"):
        actual = (site / "2026-06" / rel).read_text()
        expected = (GOLDEN_DIR / "2026-06" / rel).read_text()
        assert actual == expected, f"Golden mismatch: {rel}"


# ---------------------------------------------------------------------------
# Spot-check values (independent of JSON formatting)
# ---------------------------------------------------------------------------


def test_metric_values(tmp_path):
    db = tmp_path / "stop_events.sqlite"
    site = tmp_path / "data"
    _make_fixture_db(db)
    compute("2026-06", db_path=db, data_root=tmp_path / "rawdata", site_data_dir=site)

    idx = json.loads((site / "2026-06" / "index.json").read_text())
    assert len(idx) == 1
    row = idx[0]
    assert row["grade"] == "C"
    assert row["median"] == 3.0
    assert row["p90"] == 5.0
    assert row["served_pct"] == 100.0
    assert row["ontime_pct"] == 50.0
    assert row["score"] == 60.8
    assert row["sample_n"] == 304
    assert row["trend_mom"] is None

    line = json.loads((site / "2026-06" / "line" / "R1.json").read_text())
    d0 = line["per_direction"]["0"]
    assert d0["grade"] == "C"
    assert d0["median"] == 3.0
    assert d0["p90"] == 5.0

    dist = line["distribution_buckets"]
    assert dist[0] == {"count": 0, "label": "<-1m"}
    assert dist[1] == {"count": 152, "label": "-1..2m"}
    assert dist[2] == {"count": 0, "label": "2..5m"}
    assert dist[3] == {"count": 152, "label": "5..10m"}

    hm = line["heatmap"]
    # Mon Jun 1 (delay=60→1.0 min) + Mon Jun 8 (delay=300→5.0 min) → median 3.0
    assert hm[0][4] == 3.0
    assert hm[1][4] == 1.0  # Tue Jun 2, delay=60
    assert hm[4][4] == 5.0  # Fri Jun 5, delay=300
    assert hm[0][0] is None  # no data at Mon hour 5

    weekly = line["weekly"]
    assert len(weekly) == 2
    assert weekly[0] == {"median": 1.0, "p90": 5.0, "week_start": "2026-06-01"}
    assert weekly[1] == {"median": 5.0, "p90": 5.0, "week_start": "2026-06-08"}

    assert line["caveats"] == []
    assert json.loads((site / "2026-06" / "feed_health.json").read_text()) == {"days": []}


def test_compute_all_writes_months_json(tmp_path):
    db = tmp_path / "stop_events.sqlite"
    site = tmp_path / "data"
    _make_fixture_db(db)

    result = compute_all(db_path=db, data_root=tmp_path / "rawdata", site_data_dir=site)

    assert result["months"] == ["2026-06"]
    assert result["by_month"] == {"2026-06": 1}

    months = json.loads((site / "months.json").read_text())
    assert months == ["2026-06"]
    assert (site / "2026-06" / "index.json").exists()


def test_min_sample_excluded(tmp_path):
    """Route with < 300 measured events gets grade — and is excluded from index."""
    db = tmp_path / "stop_events.sqlite"
    site = tmp_path / "data"
    con = sqlite3.connect(str(db))
    with con:
        con.executescript(SCHEMA_SQL)
        # 5 stops only — well below MIN_SAMPLE=300
        for j in range(1, 6):
            con.execute(
                "INSERT INTO stop_events VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-06-01", "X1", 0, "TX1", f"S{j}", j, 1780293600 + j * 60, 60, 0),
            )
    con.close()

    compute("2026-06", db_path=db, data_root=tmp_path / "raw", site_data_dir=site)

    idx = json.loads((site / "2026-06" / "index.json").read_text())
    assert idx == []  # excluded from ranking

    line = json.loads((site / "2026-06" / "line" / "X1.json").read_text())
    assert line["per_direction"]["0"]["grade"] == "—"
    assert line["per_direction"]["0"]["median"] is None
