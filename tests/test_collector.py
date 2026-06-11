"""Unit tests for collector/collector.py."""

import signal
from unittest.mock import MagicMock, patch

import pytest
import zstandard as zstd
from google.transit import gtfs_realtime_pb2

import collector.collector as col

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_feed(header_ts: int, n_entities: int = 1) -> bytes:
    """Build a minimal FeedMessage protobuf."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = header_ts
    for i in range(n_entities):
        entity = feed.entity.add()
        entity.id = str(i)
        entity.trip_update.trip.trip_id = f"TRIP-{i}"
    return feed.SerializeToString()


@pytest.fixture(autouse=True)
def reset_shutdown(monkeypatch):
    """Ensure _shutdown is False before each test."""
    monkeypatch.setattr(col, "_shutdown", False)


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(col, "DATA_ROOT", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# write_snapshot
# ---------------------------------------------------------------------------


def test_write_snapshot_atomic(data_root):
    path = data_root / "out.pb.zst"
    col.write_snapshot(path, b"hello world")
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()


def test_write_snapshot_compresses_correctly(data_root):
    raw = make_feed(12345)
    path = data_root / "feed.pb.zst"
    col.write_snapshot(path, raw)
    dctx = zstd.ZstdDecompressor()
    assert dctx.decompress(path.read_bytes()) == raw


# ---------------------------------------------------------------------------
# service_date (04:00 boundary)
# ---------------------------------------------------------------------------


def test_service_date_after_midnight_before_4am():
    # 2026-06-11 02:00 Sofia = still 2026-06-10 service date
    from datetime import datetime
    from zoneinfo import ZoneInfo

    dt = datetime(2026, 6, 11, 2, 0, tzinfo=ZoneInfo("Europe/Sofia"))
    assert col.service_date(dt.timestamp()) == "2026-06-10"


def test_service_date_after_4am():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    dt = datetime(2026, 6, 11, 5, 0, tzinfo=ZoneInfo("Europe/Sofia"))
    assert col.service_date(dt.timestamp()) == "2026-06-11"


# ---------------------------------------------------------------------------
# poll_once — dedup
# ---------------------------------------------------------------------------


def make_stats() -> dict:
    feeds = ["tripupdates", "vehiclepositions", "alerts"]
    return {
        "last_poll": {f: 0.0 for f in feeds},
        "last_header_ts": {f: 0 for f in feeds},
        "failures": {f: 0 for f in feeds},
        "snapshots": {f: 0 for f in feeds},
        "gap_max_24h": 0.0,
    }


def test_poll_once_dedup_skips_write(data_root):
    """Same header.timestamp → no snapshot written."""
    raw = make_feed(99999)
    last_header_ts = {"tripupdates": 99999}
    stats = make_stats()

    with patch.object(col, "fetch_feed", return_value=(200, raw)):
        col.poll_once("tripupdates", "http://x", last_header_ts, stats)

    assert stats["snapshots"]["tripupdates"] == 0


def test_poll_once_new_ts_writes_snapshot(data_root):
    """New header.timestamp → snapshot file created."""
    raw = make_feed(100000)
    last_header_ts = {"tripupdates": 99999}
    stats = make_stats()

    with patch.object(col, "fetch_feed", return_value=(200, raw)):
        col.poll_once("tripupdates", "http://x", last_header_ts, stats)

    assert stats["snapshots"]["tripupdates"] == 1
    assert last_header_ts["tripupdates"] == 100000


def test_poll_once_http_error_increments_failures(data_root):
    stats = make_stats()
    with patch.object(col, "fetch_feed", return_value=(500, b"")):
        col.poll_once("tripupdates", "http://x", {}, stats)
    assert stats["failures"]["tripupdates"] == 1
    assert stats["snapshots"]["tripupdates"] == 0


def test_poll_once_network_exception_increments_failures(data_root):
    stats = make_stats()
    with patch.object(col, "fetch_feed", side_effect=OSError("connection refused")):
        col.poll_once("tripupdates", "http://x", {}, stats)
    assert stats["failures"]["tripupdates"] == 1


def test_poll_once_zero_header_ts_still_writes(data_root):
    """header.timestamp == 0 (protobuf default) must still write a snapshot."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    # timestamp intentionally not set → defaults to 0
    raw = feed.SerializeToString()
    stats = make_stats()

    with patch.object(col, "fetch_feed", return_value=(200, raw)):
        col.poll_once("tripupdates", "http://x", {}, stats)

    assert stats["snapshots"]["tripupdates"] == 1


# ---------------------------------------------------------------------------
# SIGTERM handler
# ---------------------------------------------------------------------------


def test_sigterm_sets_shutdown_flag(monkeypatch):
    monkeypatch.setattr(col, "_shutdown", False)
    col._handle_sigterm(signal.SIGTERM, None)
    assert col._shutdown is True


# ---------------------------------------------------------------------------
# fetch_static_gtfs
# ---------------------------------------------------------------------------


def test_fetch_static_gtfs_skips_if_exists(tmp_path, monkeypatch):
    from collector.fetch_static_gtfs import fetch

    monkeypatch.setattr("collector.fetch_static_gtfs.DATA_ROOT", tmp_path)
    dest = tmp_path / "gtfs" / "2026-06-11.zip"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"PK fake")

    with patch("collector.fetch_static_gtfs.requests.get") as mock_get:
        result = fetch("2026-06-11")

    mock_get.assert_not_called()
    assert result == dest


def test_fetch_static_gtfs_downloads_and_renames(tmp_path, monkeypatch):
    from collector.fetch_static_gtfs import fetch

    monkeypatch.setattr("collector.fetch_static_gtfs.DATA_ROOT", tmp_path)
    fake_zip = b"PK" + b"\x00" * 100

    mock_response = MagicMock()
    mock_response.content = fake_zip
    mock_response.raise_for_status = MagicMock()

    with patch("collector.fetch_static_gtfs.requests.get", return_value=mock_response):
        result = fetch("2026-06-11")

    assert result.exists()
    assert result.read_bytes() == fake_zip
    assert not result.with_suffix(".tmp").exists()


def test_fetch_static_gtfs_rejects_non_zip(tmp_path, monkeypatch):
    from collector.fetch_static_gtfs import fetch

    monkeypatch.setattr("collector.fetch_static_gtfs.DATA_ROOT", tmp_path)

    mock_response = MagicMock()
    mock_response.content = b"not a zip"
    mock_response.raise_for_status = MagicMock()

    with patch("collector.fetch_static_gtfs.requests.get", return_value=mock_response):
        with pytest.raises(ValueError, match="not a ZIP"):
            fetch("2026-06-11")
