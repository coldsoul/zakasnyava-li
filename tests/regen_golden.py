#!/usr/bin/env python3
"""Regenerate golden files for test_compute_metrics.py.

Run from repo root: python tests/regen_golden.py
"""

import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.compute_metrics import compute

SCHEMA_SQL = (Path(__file__).parent.parent / "pipeline" / "schemas.sql").read_text()
GOLDEN_DIR = Path(__file__).parent / "golden"

# 2026-06-{01..08} 09:00 EEST = 06:00 UTC  (UTC+3 in summer)
BASE_TS = {
    "2026-06-01": 1780293600,
    "2026-06-02": 1780380000,
    "2026-06-03": 1780466400,
    "2026-06-04": 1780552800,
    "2026-06-05": 1780639200,
    "2026-06-06": 1780725600,
    "2026-06-07": 1780812000,
    "2026-06-08": 1780898400,
}


def make_fixture_db(db_path: Path) -> None:
    """8 trips × 38 stops = 304 measured events. T1-T4: delay 60 s. T5-T8: delay 300 s."""
    con = sqlite3.connect(str(db_path))
    with con:
        con.executescript(SCHEMA_SQL)
        rows = []
        for i, (date_str, base) in enumerate(BASE_TS.items()):
            trip_id = f"T{i + 1}"
            delay = 60 if i < 4 else 300
            for j in range(1, 39):
                rows.append((date_str, "R1", 0, trip_id, f"S{j}", j, base + j * 60, delay, 0))
        con.executemany(
            "INSERT INTO stop_events "
            "(service_date, route_id, direction_id, trip_id, stop_id, "
            " stop_sequence, scheduled_ts, delay_sec, is_ghost) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    con.close()


def main():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db = tmp_path / "stop_events.sqlite"
        site = tmp_path / "data"
        make_fixture_db(db)
        compute("2026-06", db_path=db, data_root=tmp_path / "rawdata", site_data_dir=site)

        month_golden = GOLDEN_DIR / "2026-06"
        month_golden.mkdir(parents=True, exist_ok=True)
        (month_golden / "line").mkdir(exist_ok=True)

        for src, dst in [
            (site / "2026-06" / "index.json", month_golden / "index.json"),
            (site / "2026-06" / "line" / "R1.json", month_golden / "line" / "R1.json"),
            (site / "2026-06" / "feed_health.json", month_golden / "feed_health.json"),
        ]:
            shutil.copy(src, dst)
            print(f"wrote {dst}")


if __name__ == "__main__":
    main()
