# Sofia Transit Reliability — v1 Implementation Spec

**Working title:** "Закъснява ли?" (zakasnyava-li)
**Audience for this document:** an LLM coding agent implementing v1 end-to-end, story by story.
**Status:** draft for v1. Anything marked `v2` is explicitly out of scope — do not build it.

---

## 1. What we are building

A static website showing how reliable each public transport line in Sofia actually is,
computed from the city's open GTFS / GTFS-Realtime feeds.

- Homepage: ranking of lines, worst-first, with grade A–E.
- Line page: verdict sentence, metric cards, delay distribution chart,
  hour×day heatmap, weekly trend chart.
- All numbers are precomputed once a day. The website has **no backend** —
  it serves static HTML + JSON + charts rendered client-side.

**Core value:** history. The product is "how late is line 94 *usually*",
not "where is my bus now". Many live maps exist; no historical reliability site does.

### Important architectural note (read first)

"Fetch once a day" applies to **aggregation and site publishing**, not raw collection.
GTFS-Realtime is ephemeral — a TripUpdates snapshot describes only the current moment.
To know what happened during a day, a small **collector must poll the RT feed all day**
(every ~20s) and append snapshots to disk. The nightly job then processes the
accumulated snapshots, computes metrics, and rebuilds the static site.

So v1 has exactly two scheduled processes:

1. `collector` — long-running poller (20s-interval loop under systemd), appends raw snapshots.
2. `nightly` — batch job triggered by a **systemd timer** (not crontab) at ~03:10
   Europe/Sofia: snapshots → stop events → metrics → static site → deploy.
   It is stateless and idempotent; observability is freshness-based (see §8).

Raw snapshots are the **source of truth**. Never delete them in v1.
If matching logic changes, we replay and recompute.

---

## 2. Data sources (CONFIRMED 2026-06-10/11 — see RECON.md)

| Source | What | Format | Notes |
|---|---|---|---|
| `https://gtfs.sofiatraffic.bg/api/v1/static` | Static GTFS zip | zip of CSVs | ~18.7 MB. Refetch daily; schedules change |
| `https://gtfs.sofiatraffic.bg/api/v1/trip-updates` | GTFS-RT **TripUpdates** | protobuf | Primary signal for delays in v1. ~80 KB night / ~930 KB rush hour |
| `https://gtfs.sofiatraffic.bg/api/v1/vehicle-positions` | GTFS-RT **VehiclePositions** | protobuf | Collected and stored, **not processed in v1** (needed for v2 true-arrival matching) |
| `https://gtfs.sofiatraffic.bg/api/v1/alerts` | GTFS-RT **Alerts** | protobuf | Collected, not processed in v1 |

### Observed feed quirks (normative — the matcher MUST handle these; do not "fix" them)

1. **No explicit delay fields.** TripUpdates publish absolute predicted
   `arrival.time` / `departure.time` only. Delay is ALWAYS computed by us as
   `predicted_time − scheduled_time`.
2. **`stop_sequence` is not populated** (protobuf default 0 everywhere).
   Stop matching key is `(trip_id, stop_id)`; when a trip visits the same stop
   more than once (loop routes), disambiguate by choosing the scheduled time
   **closest to the RT predicted time**.
3. **`trip.start_date` and `trip.start_time` are empty.** Service date must be
   inferred from the feed `header.timestamp` (Europe/Sofia, 04:00 boundary, §4).
4. **No `calendar.txt`** in the static zip — service days are defined entirely by
   `calendar_dates.txt`. Expanding the active trip set for a date is a direct
   lookup, and is MANDATORY before ghost computation (counting trips from
   inactive services as ghosts inflated ghost rate ~5–10× in the spike).
5. `header.timestamp` is ~1 s fresh at all observed times; trip_id join rate
   to `trips.txt` was 100% in both night and rush-hour samples.

Rules:

- Use the official `gtfs-realtime-bindings` protobuf library to parse RT feeds. Do not hand-parse protobuf.
- Send a descriptive `User-Agent` header (project name + contact URL). Be a polite client.
- If a fetch fails, log and continue. Never crash the collector on a bad response.
- Facts above were confirmed empirically; RECON.md is the source of record.
  If the feed contradicts RECON.md at implementation time, stop and report — do not guess.

---

## 3. Tech stack

- **Language for collector + processing:** Python 3.11+. Dependencies: `requests`,
  `gtfs-realtime-bindings`, `pandas` (or `polars`), `zstandard`. Keep it boring.
- **Storage:** filesystem + SQLite. No server databases in v1.
  - Raw snapshots: `data/raw/YYYY-MM-DD/tripupdates/HHMMSS.pb.zst` (and `vehiclepositions/`, `alerts/`).
  - Static GTFS archive: `data/gtfs/YYYY-MM-DD.zip`.
  - Derived: `data/derived/stop_events.sqlite`.
  - Published: `site/public/data/months.json` + `site/public/data/YYYY-MM/{index,feed_health}.json` + `site/public/data/YYYY-MM/line/{id}.json`.
- **Site:** Astro (static build) + Chart.js for charts. Plain CSS, no framework.
  Bulgarian UI text. No analytics, no cookies, no tracking — privacy-first like ms-navigator.bg.
- **Hosting:** any static host (Cloudflare Pages / GitHub Pages / Netlify). Collector + nightly timer on a small VPS.

Expected volumes (sanity bounds, verify in Story 0): TripUpdates+VehiclePositions raw
≈ 100–500 MB/day compressed; stop events ≈ 400–600k rows/day, ≈ 30–40 MB/day in SQLite.
If observed volume exceeds 5× these bounds, stop and report instead of silently filling the disk.

---

## 4. Definitions (normative — implement exactly)

These definitions are user-facing methodology. Do not improvise alternatives.

- **Stop event:** one row per (service_date, route_id, direction_id, trip_id, stop_id,
  stop_sequence) with `scheduled_time` and `observed_delay_sec`. stop_sequence comes
  from the **static** stop_times.txt row that was matched (RT does not provide it).
- **Observed delay (v1 method):** delay is computed, never read:
  `delay = predicted_time − scheduled_time`, where predicted_time is the arrival
  (fallback: departure) time from the **last TripUpdate received at or before the
  scheduled time + 30 min window** — i.e. the final prediction CGM made for that stop.
  Matching key is `(trip_id, stop_id)`; for trips visiting a stop more than once,
  pick the scheduled stop_times row whose time is closest to predicted_time.
  Discard |delay| > 3 h as feed noise (log a counter). This is an approximation of
  actual arrival; the limitation must be stated on the methodology page.
  (`v2`: compute true arrivals from VehiclePositions geofencing.)
- **Active trip set for a service date:** trips whose `service_id` is active per
  `calendar_dates.txt` for that date (there is no calendar.txt). All scheduling,
  ghost detection, and served % computations operate ONLY on the active set.
- **On-time:** delay within **[−60, +180] seconds** (early by ≤1 min through late by ≤3 min).
  Early departures beyond −60s are *not* on-time — they count as a distinct failure mode.
- **Ghost trip:** a trip in the **active trip set** for that service date for which
  **no TripUpdate entity was ever observed** within ±30 min of the trip's first scheduled
  departure. Ghost trips are excluded from delay stats and included in served %.
  Sanity bound: if a line's ghost rate exceeds 20% for a day, flag it for review —
  the spike showed rates that high indicate a calendar-filtering bug, not reality.
- **Served %** = (scheduled trips − ghost trips) / scheduled trips × 100, per line per period.
- **Median / p90 delay:** computed over stop events of measured (non-ghost) trips,
  per (route, direction), then the displayed line-level number is the **worse direction**.
  Use linear interpolation percentiles. Clamp negative delays to their real value (do not
  floor at 0 — early matters).
- **Minimum sample:** a (line, month) with **< 300 measured stop events** gets grade `—`
  ("недостатъчно данни") and is excluded from the ranking.
- **Frequency-based lines caveat (v1):** lines whose scheduled peak headway ≤ 10 min
  (metro, core trams) still get schedule-based stats in v1, but their line page must show
  a caveat banner: "Честа линия — точността спрямо разписание е по-малко показателна."
  (`v2`: excess-waiting-time metric for these lines.)
- **Period:** calendar month, Europe/Sofia timezone. Service date boundary at 04:00 local
  (a 01:30 night departure belongs to the previous service date).

### Grade formula (0–100 points → letter)

```
score = 0.40 * served_component + 0.35 * ontime_component + 0.25 * p90_component

served_component = clamp01((served_pct − 90) / 10) * 100          # 90% → 0, 100% → 100
ontime_component = clamp01((ontime_pct − 50) / 45) * 100           # 50% → 0, 95% → 100
p90_component    = clamp01((15 − p90_minutes) / 12) * 100          # 15min → 0, 3min → 100
```

Thresholds: **A ≥ 85, B ≥ 70, C ≥ 55, D ≥ 40, E < 40.**
Components are computed per (route, direction); the line's score = worse direction.
The formula and thresholds are published verbatim on the methodology page and must not
change mid-season.

---

## 5. Repository layout

```
zakasnyava-li/
  collector/
    collector.py          # RT polling loop
    fetch_static_gtfs.py  # daily static GTFS download
  pipeline/
    build_stop_events.py  # raw snapshots -> stop_events.sqlite
    compute_metrics.py    # stop_events -> per-line JSON
    schemas.sql
  site/                   # Astro project
    src/pages/index.astro         # ranking
    src/pages/line/[id].astro     # line page (one per line+direction, prerendered)
    src/pages/methodology.astro
    public/data/                  # generated JSON (gitignored); months.json + YYYY-MM/ subdirs
  ops/
    nightly.sh            # batch entrypoint: fetch gtfs -> pipeline -> astro build -> deploy
    systemd/              # collector.service, nightly.service, nightly.timer
  tests/
  data/                   # gitignored
  README.md
  SPEC.md                 # this file
```

---

## 6. Data schemas

`stop_events` (SQLite):

```sql
CREATE TABLE stop_events (
  service_date TEXT NOT NULL,        -- YYYY-MM-DD (Europe/Sofia, 04:00 boundary)
  route_id TEXT NOT NULL,
  direction_id INTEGER NOT NULL,
  trip_id TEXT NOT NULL,
  stop_id TEXT NOT NULL,
  stop_sequence INTEGER NOT NULL,
  scheduled_ts INTEGER NOT NULL,     -- unix epoch
  delay_sec INTEGER,                 -- NULL when trip is ghost
  is_ghost INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (service_date, trip_id, stop_sequence)
);
CREATE INDEX idx_se_route ON stop_events(route_id, direction_id, service_date);
```

Published JSON (one file per line keeps pages cacheable):

```
site/public/data/months.json            # ["YYYY-MM", ...] newest-first — drives month selector
site/public/data/YYYY-MM/index.json     # ranking: [{line_id, name, type, median, p90,
                                        #   served_pct, ontime_pct, score, grade, trend_mom, sample_n}]
site/public/data/YYYY-MM/line/{id}.json # {meta, per_direction: {0:{...},1:{...}},
                                        #   distribution_buckets, heatmap[7][19],
                                        #   weekly: [{week_start, median, p90}], caveats: []}
site/public/data/YYYY-MM/feed_health.json # {days: [{date, snapshots_expected, snapshots_ok, pct}]}
```

Distribution buckets (fixed): `<−1 min`, `−1..2`, `2..5`, `5..10`, `10..15`, `15+` (minutes).
Heatmap: rows Mon..Sun, columns hours 05..23, value = median delay minutes for that cell,
`null` when cell has < 20 events. Round all published numbers to 1 decimal.

---

## 7. Stories (implement in order; each has acceptance criteria)

### Story 0 — Feed reconnaissance (do this before any other code)

Write a throwaway script `ops/recon.py` that, for 10 minutes:
fetches each candidate feed URL every 20s, records HTTP status, payload bytes,
`header.timestamp`, entity counts, and 5 sample `trip_id`s; then downloads the static
GTFS and checks whether RT trip_ids exist in `trips.txt`.

**Accept:** a written `RECON.md` with: confirmed URLs, observed update interval,
median payload size, trip_id match rate (must be > 80%, else stop and report),
and any auth/rate-limit observations. **All later stories may rely only on facts
recorded in RECON.md, not on assumptions.**

> **STATUS: DONE (2026-06-10/11).** `RECON.md` exists in the repo and was produced
> from a live recon plus a 9.7-hour overnight+rush-hour capture (125,541 matched
> stop events). Do not redo Story 0; read RECON.md and proceed to Story 1.

### Story 1 — Collector

`collector.py`: loop every 20s; fetch TripUpdates, VehiclePositions, Alerts;
skip write if `header.timestamp` unchanged from previous poll (dedup);
write zstd-compressed protobuf to the raw layout above; structured log line per cycle.
Each cycle also updates the collector's textfile metrics (see §8).
Plus `fetch_static_gtfs.py` storing the daily zip. Systemd unit with restart=always.

Hardening requirements (each one corresponds to a failure observed in the spike):

- **Hard per-request deadline.** Library connect/read timeouts are NOT sufficient —
  the spike's poller hung permanently on one request after ~9 h despite
  `timeout=25` (a trickling response defeats read timeouts). Enforce a wall-clock
  cap per cycle: either a watchdog that aborts the request, or systemd
  `WatchdogSec=` with `sd_notify` keepalives from the loop, or both.
- **Atomic, one-file-per-snapshot writes** (write to tmp, rename). Never hold a
  long-lived append stream open — a SIGTERM mid-write corrupted the spike's
  gzip JSONL. Handle SIGTERM gracefully (finish current write, exit 0).
- **`MemoryMax=512M`** in the unit file. The spike poller peaked at 438 MB RSS
  for no good reason; cap it and let systemd restart on breach.
- **Stdout must be line-buffered** (`PYTHONUNBUFFERED=1` in the unit) so journald
  shows output in real time.

**Accept:** runs 24h unattended; gaps > 5 min are visible in logs; disk usage within
bounds of §3; `feed_health` computable from file timestamps alone; §8 collector
metrics present and updating; kill -STOP on a fake slow server does not stall
collection beyond one watchdog interval; SIGTERM leaves no partial files.

### Story 2 — Stop events builder

`build_stop_events.py --date YYYY-MM-DD`: load that service date's static GTFS,
expand the **active trip set from `calendar_dates.txt`** (there is no calendar.txt —
see §2 quirk 4; this filter is mandatory before any ghost computation), stream the
day's TripUpdates snapshots chronologically, and apply the §4 definitions to fill
`stop_events`. Matching per §2/§4: join on `(trip_id, stop_id)`, nearest scheduled
time for multi-visit stops, delay computed from absolute predicted times, service
date inferred from header timestamps. Idempotent: re-running a date replaces that
date's rows. Handle: trips spanning midnight, `schedule_relationship`
SKIPPED/CANCELED (canceled trip = ghost; skipped stop = no event), duplicate
trip_ids (log, keep first), RT stops not present in static stop_times (log, drop),
**truncated or corrupt snapshot files (log, skip — never abort the day)**.

**Accept:** unit tests with synthetic fixtures for: normal trip, ghost trip,
canceled trip, early running, midnight span, loop route visiting a stop twice,
trip whose service is inactive that date (must NOT be counted as ghost),
truncated snapshot file. A real day processes in < 10 min on a 2-vCPU box.
Row counts within §3 bounds; per-line ghost rate sanity check from §4 applied.

### Story 3 — Metrics & grades

`compute_metrics.py --month YYYY-MM`: produce the JSON files of §6 exactly per
the §4 formulas, including weekly series (ISO weeks clipped to month), month-over-month
trend (`trend_mom` = this month's median − previous month's, null if no prior month),
caveat flags (frequency-based line, low sample), and feed_health.

**Accept:** golden-file tests: a fixed synthetic stop_events fixture must produce
byte-identical JSON. Grade boundary cases tested (score 85.0 → A, 84.9 → B).
No published number with more than 1 decimal.

### Story 4 — Static site

Astro site, Bulgarian UI, two real pages + methodology:

- `index.astro`: ranking table worst-first (columns: линия, медиана, p90, изпълнени,
  тренд, оценка), grade badges colored A=teal → E=red, month selector (links to
  `/2026-05/` style static paths), link to feed health.
- `line/[id].astro`: per-direction tabs; verdict sentence generated from a fixed
  template by grade band (templates included in repo, Bulgarian); 4 metric cards
  (медиана, p90, изпълнени, навреме ±[−1,+3]); distribution bar chart; hour×day
  heatmap (CSS grid, darker = worse, gray = insufficient data); weekly trend line
  chart (median solid, p90 dashed); caveat banners.
- `methodology.astro`: the §4 definitions and grade formula, verbatim, in Bulgarian.

Charts: Chart.js, data loaded from the prebuilt JSON at build time (inline it) —
the deployed site must work with JavaScript disabled except for chart rendering.
Mobile-first; the ranking table must be usable at 380px width.

**Accept:** `astro build` succeeds with the Story 3 fixture JSON; Lighthouse
performance ≥ 95 on the line page; no external requests except self + cdn for Chart.js
(or bundle Chart.js — preferred); zero cookies/localStorage.

### Story 5 — Nightly orchestration, observability & deploy

`ops/nightly.sh`: fetch static GTFS → build yesterday's stop events → recompute
all months metrics (`--all-months`) → `astro build` → deploy via orphan git push to
`gh-pages` branch. On any failure, exit nonzero; on success, ping the dead man's
switch as the **last** step.

Scheduling: `nightly.timer` + `nightly.service` (systemd, **not crontab**) with
`OnCalendar=*-*-* 03:10:00 Europe/Sofia`, `Persistent=true` (missed runs execute
after reboot). systemd's default behavior prevents overlapping runs.

Implement all of §8. Document VPS setup in README: unit files, timer, node_exporter
textfile collector path, dead man's switch URL configuration, disk monitoring with
a 90% alarm.

**Accept:** a full dry run from a day of collected raw data to a deployed site
completes hands-off; a deliberately broken snapshot file does not abort the run
(logged and skipped); `systemctl list-timers` shows the schedule;
killing the run mid-pipeline triggers exactly one notification and leaves the
previously deployed site intact; suppressing the success ping triggers the dead
man's switch alert; all §8 metrics appear in the textfile after one run.

---

## 8. Observability (normative)

Principle: for the batch pipeline, monitor **freshness of results**, not process
liveness. For the collector, monitor **liveness and staleness**. Three mechanisms,
all mandatory:

### 8.1 Dead man's switch (catches "never ran" and "ran and failed")

- The final step of a *successful* `nightly.sh` is `curl -fsS $DEADMAN_URL`
  (healthchecks.io or self-hosted Uptime Kuma; URL via environment, never committed).
- The monitor's expected period is 24h with a 3h grace window; it alerts on absence.
- Alerting must never depend solely on code that runs inside the job itself.

### 8.2 Metrics via node_exporter textfile collector

Write atomically (tmp file + rename) to the textfile collector directory.

`nightly.prom` (written at the end of every run, success or failure):

```
nightly_last_run_timestamp_seconds
nightly_last_success_timestamp_seconds
nightly_last_exit_status
nightly_duration_seconds{stage="fetch_gtfs|stop_events|metrics|build|deploy"}
nightly_stop_events_rows
nightly_ghost_trips_total
nightly_lines_graded
```

`collector.prom` (updated every cycle):

```
collector_last_poll_timestamp_seconds{feed="tripupdates|vehiclepositions|alerts"}
collector_last_feed_header_timestamp_seconds{feed=...}
collector_poll_failures_total{feed=...}
collector_snapshots_written_total{feed=...}
collector_gap_seconds_max_24h
```

Minimum alert rules (Prometheus, or equivalent if owner chooses another stack):
`time() - nightly_last_success_timestamp_seconds > 100000` (≈28h),
`time() - collector_last_feed_header_timestamp_seconds > 600` during service hours
(05:00–24:00 Sofia) — distinguishes "our poller is broken" from "CGM's feed is stale"
by pairing with `collector_last_poll_timestamp_seconds`.

### 8.3 Logs

- Both processes log to journald via systemd (no log files to rotate).
- Every nightly run emits exactly one final JSON summary line: date processed,
  per-stage durations, row counts, ghost count, exit status.
- Collector emits one structured line per cycle at debug level, and warn-level
  lines for gaps > 5 min or repeated fetch failures.

### 8.4 Public observability

`feed_health.json` and the site's build timestamp (visible in the footer) expose
pipeline health to visitors. A site that grades CGM's reliability must show its own.

---

## 9. Out of scope for v1 (do not build, do not stub)

- Stop-level drill-down pages.
- True arrival computation from VehiclePositions (collect the data, nothing more).
- Excess-waiting-time metric for frequent lines (caveat banner only).
- Accounts, comments, API for third parties, English version.
- Real-time anything on the site.
- Weighting by ridership, route length, or weather/incident exclusions.

## 10. Open questions (resolve with the project owner before Story 4)

1. Final site name and domain.
2. Grade thresholds and on-time window — confirm after seeing one real month of data
   (the formula is fixed *per season*, but the first calibration is allowed once).
3. Which line types to include at launch (bus/tram/trolley yes; metro likely caveated
   or excluded until headway metric exists).
4. Hosting choice for the static site and the VPS provider for the collector.
5. Monitoring stack: hosted healthchecks.io + no Prometheus (textfile metrics still
   written, scraped later), or full node_exporter + Prometheus + Alertmanager from day 1.

## 11. Tone & values (applies to all user-facing text)

Neutral, factual, reproducible. No mockery of CGM or drivers; the data speaks.
Every number on the site must be traceable to the methodology page. Bulgarian,
plain language, short sentences. Privacy-first: no tracking of visitors, ever.
