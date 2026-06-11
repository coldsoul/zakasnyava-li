# RECON.md — Sofia GTFS feeds, empirical findings

**Recon performed:** 2026-06-10 (evening) and 2026-06-11 (overnight capture + morning rush).
**Method:** tier0_recon.py (endpoint probe) + tier1_peak.py (9.7 h TripUpdates capture,
125,541 matched stop events). This file is the **source of record** for feed facts.
Later implementation stories rely on these facts, not on assumptions or the GTFS-RT
spec's optional fields. If the live feed contradicts this file, stop and report.

---

## 1. Confirmed endpoints

| Feed | URL | Confirmed |
|---|---|---|
| Static GTFS zip | `https://gtfs.sofiatraffic.bg/api/v1/static` | HTTP 200, ~18.7 MB, valid zip |
| TripUpdates | `https://gtfs.sofiatraffic.bg/api/v1/trip-updates` | HTTP 200, protobuf |
| VehiclePositions | `https://gtfs.sofiatraffic.bg/api/v1/vehicle-positions` | HTTP 200, protobuf |
| Alerts | `https://gtfs.sofiatraffic.bg/api/v1/alerts` | HTTP 200, protobuf |

No authentication required. No rate limiting observed at 1 poll / 30 s over ~9 h
(but see §5 incident). `content-type: application/octet-stream` for RT,
`application/zip` for static.

## 2. Feed characteristics

- **Freshness:** `header.timestamp` was 0–1 s old on every manual probe (night and
  rush hour). The feed regenerates at least every few seconds; polling every 20–30 s
  misses essentially nothing.
- **Scale:** TripUpdates ~81 KB / 161 trip entities at ~21:00 UTC (midnight Sofia);
  ~932 KB / 1,326 entities at ~06:10 UTC (09:10 Sofia, rush hour).
  VehiclePositions: 127 vehicles at night, 590 at rush hour.
  Note: active trips ≈ 2.2× vehicle positions at peak — not all trips map 1:1 to a
  GPS-reporting vehicle; worth revisiting when v2 does position-based matching.
- **Static GTFS contents:** agency, calendar_dates, fare_attributes, feed_info,
  levels, pathways, routes, shapes, stop_times, stops, transfers, translations, trips.
  **No `calendar.txt`.** 30,616 trip_ids, 140 route_ids (includes metro: levels/pathways present).

## 3. Critical data-model facts (drive the matcher design)

1. **trip_id join: 100%.** Every RT trip_id existed in static `trips.txt`, in both
   the night sample (161/161) and the rush-hour sample (1,326/1,326).
   Format example: `A57-A290-6-27-5461188771` (route prefix visible: A=bus,
   TB=trolleybus, TM=tram).
2. **No explicit delay fields.** `stop_time_update.arrival/departure` carry absolute
   predicted `time` only, never `delay`. Delay must always be computed as
   `predicted_time − scheduled_time`.
3. **`stop_sequence` is not populated** (0 everywhere = protobuf default).
   Stop matching must use `(trip_id, stop_id)`. In a 5,000-record sample this pair
   matched static stop_times **5000/5000**; the triple with sequence matched 0/5000.
   For loop trips visiting a stop twice, disambiguate by nearest scheduled time
   to the predicted time.
4. **`trip.start_date` and `trip.start_time` are empty strings.** Service date must
   be inferred from `header.timestamp` (Europe/Sofia, 04:00 service-day boundary).
5. **`schedule_relationship` was 0 (SCHEDULED) for all observed entities.**
   No CANCELED/SKIPPED seen yet; ghost detection must work by absence, and the
   pipeline must still handle 1/3 values when they eventually appear.
6. **stop_id formats match** between RT and static (e.g. `A2743`, `TM0300`).

## 4. Spike measurement results (sanity, not publication-grade)

Capture window 582 min (overnight + morning rush, 2026-06-10 21:11 → 06:53 UTC).
125,541 stop events matched. Example lines (combined directions):

| line | events | median | p90 | on-time [−1,+3] |
|---|---|---|---|---|
| 95 | 1,026 | 0.5 min | 12.7 min | 63% |
| 94 | 1,817 | 0.5 min | 4.0 min | 77% |

Interpretation: medians near zero, fat differentiated tails — exactly the shape the
p90-centric methodology assumes. Delay signal is real and discriminates between lines.

**Ghost rates from the spike (20–29%) are NOT real.** The spike script did not filter
trips by `calendar_dates.txt` service activity, so trips belonging to services not
active that day were falsely counted as ghosts. The production matcher MUST expand
the active trip set first. Treat any per-line daily ghost rate > 20% as a probable
calendar-filtering bug.

## 5. Operational incidents observed (drive collector hardening)

1. **Permanent hang after ~8.7 h.** The spike poller stopped producing snapshots at
   ~05:53 UTC while remaining a live process; the feed itself was confirmed healthy
   at the time. `requests` with `timeout=25` did not protect against it (read
   timeouts reset on every received byte; a stalled/trickling response can hang
   forever). → Collector needs a hard wall-clock cap per cycle and/or systemd
   `WatchdogSec` keepalives.
2. **SIGTERM corrupted a long-lived gzip stream.** `systemctl stop` killed the
   process without running cleanup; the append-mode `.jsonl.gz` was left without an
   end-of-stream marker and required shell salvage (`zcat 2>/dev/null`).
   → Production writes one file per snapshot, atomically (tmp + rename); handle
   SIGTERM gracefully. Raw one-shot `.pb` files survived intact — raw-as-source-of-truth
   validated in practice.
3. **Unexplained memory peak: 438 MB RSS** in a loop with no accumulating state.
   → `MemoryMax=512M` in the unit; let systemd restart on breach.
4. **Python stdout block-buffers under journald**, hiding logs for long periods.
   → `PYTHONUNBUFFERED=1` in unit files.

## 6. Volume measurements (replace the spec's estimates)

- TripUpdates snapshot: ~80 KB (night) to ~930 KB (rush). At 30 s polling with
  dedup, raw TripUpdates ≈ 0.5–1.5 GB/day **uncompressed**; zstd should bring this
  to ~100–300 MB/day (protobuf with heavy redundancy between snapshots).
- VehiclePositions adds ~15–70 KB per snapshot — minor in comparison.
- Stop events: ~125k matched events in 9.7 h including the thin overnight period →
  expect ~350–550k/day full-service, consistent with the spec's §3 bounds.

## 7. Open items for the implementation

- Measure the actual `header.timestamp` change interval precisely (probe suggests
  ≤ a few seconds; confirm and record here).
- Confirm whether `direction_id` in RT (`dir` field) is populated meaningfully
  (the spike recorded it but did not validate against static trips.txt).
- Investigate the 2.2× trips-to-vehicles ratio at peak before v2 position matching.
- Watch for the first CANCELED/SKIPPED entities in the wild and verify handling.
