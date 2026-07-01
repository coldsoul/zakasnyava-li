# zakasnyava-li

Static website: Sofia public transit reliability from live GTFS/GTFS-RT feeds.
Two scheduled processes: collector (polls feeds every 20s) + nightly (builds + deploys site at 03:10 Sofia). No backend.

## Stack

| Layer | Tech |
|-------|------|
| Collector + pipeline | Python 3.11+, uv |
| Storage | Filesystem (raw .pb.zst) + SQLite (derived) |
| Site | Astro (static output) + Chart.js (bundled) |
| Scheduling | systemd (not crontab) |

## Commands

```bash
uv sync --dev              # install Python deps
uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run bandit -r collector pipeline  # security scan
cd site && npm ci                    # install site deps
cd site && npm run build             # site build

# Pipeline entry-points
python pipeline/build_stop_events.py --date YYYY-MM-DD
python pipeline/build_vehicle_arrivals.py --date YYYY-MM-DD [--gtfs PATH]
python pipeline/compute_metrics.py --month YYYY-MM [--gtfs PATH] [--source tu|vp|merged]
python pipeline/compute_metrics.py --all-months [--gtfs PATH] [--source tu|vp|merged]
python pipeline/tier2_vp_recon.py --date YYYY-MM-DD [--gtfs PATH]
```

## Source of record

- **SPEC.md** — normative spec; all §4 definitions exact
- **docs/internal/RECON.md** — confirmed empirical facts about the live feeds

Feed quirks are normative — the matcher MUST handle them exactly.
If the live feed contradicts RECON.md: stop and report, do not guess.

## Before touching site code

- Bulgarian UI text
- Chart.js bundled locally (not CDN) — site must work with JS disabled except charts
- Zero cookies, zero localStorage, no external requests

## Branch and PR workflow

Each epic lives on its own feature branch. Never commit epic work directly to `main`.

```bash
git checkout -b epic/N-short-name   # one branch per epic
# implement, then merge directly to main
```

## Data flow

```
GTFS-RT feeds (every 20s)
  → collector/collector.py
  → data/raw/YYYY-MM-DD/{tripupdates,vehiclepositions,alerts}/HHMMSS.pb.zst

Static GTFS (nightly)
  → collector/fetch_static_gtfs.py
  → data/gtfs/YYYY-MM-DD.zip

Nightly pipeline (03:10 Europe/Sofia):
  pipeline/build_stop_events.py --date YYYY-MM-DD
    → data/derived/stop_events.sqlite (stop_events table, TU delays)

  pipeline/build_vehicle_arrivals.py --date YYYY-MM-DD
    → data/derived/stop_events.sqlite (vehicle_arrivals table, VP GPS delays)

  pipeline/compute_metrics.py --all-months --source merged
    → site/public/data/months.json
    → site/public/data/YYYY-MM/index.json
    → site/public/data/YYYY-MM/line/{id}.json
    → site/public/data/YYYY-MM/feed_health.json

  astro build → site/dist/ → orphan git push → gh-pages branch → GitHub Pages
```

## Feed quirks — normative, implement exactly, do not "fix"

1. **No delay fields** — v2: `delay = measured_ts − scheduled_time` (VP GPS)
   or `delay = predicted_time − scheduled_time` (TU fallback)
2. **`stop_sequence` always 0** — match on `(trip_id, stop_id)`; for loop routes pick scheduled
   stop whose time is closest to predicted_time
3. **`start_date` / `start_time` empty** — infer service date from `header.timestamp`
   (Europe/Sofia, 04:00 boundary)
4. **No `calendar.txt`** — service days from `calendar_dates.txt` only; expand active trip set
   BEFORE any ghost computation (mandatory)
5. **`schedule_relationship` = 0 always seen so far** — handle CANCELED (1) and SKIPPED (3)
   when they appear; ghost detection by absence
6. **stop_id formats match** between RT and static (e.g. `A2743`, `TM0300`) — confirmed 100% in recon

If feed contradicts RECON.md: stop and report, do not guess.

## VehiclePositions (v2 — active)

- `trip_id`, `stop_id`, `position`, `vehicle.id` all 100% populated (2026-07-01 recon, RECON.md §7)
- `current_status` always IN_TRANSIT_TO — never STOPPED_AT; cannot filter drive-bys
- Coverage: ~47% of active trips at peak (579 VP / 1,231 TU) — hybrid approach mandatory
- Matching: same `(trip_id, stop_id)` key as TU matcher; `measured_ts` = actual arrival
- GPS distance validation records haversine distance but never rejects rows

## Task tracking

All stories, epics, and progress live in **beads** (`bd`), not in this file.
Never add story status tables, TODO lists, or progress tracking here.
Use `bd ready`, `bd create`, `bd close`, etc. Beads is the single source of truth for what's done and what's next.

## Test conventions

- Framework: `pytest`; fixtures in `tests/fixtures/`; golden files in `tests/golden/`
- SQLite tests use in-memory `:memory:` — no real DB files in tests
- No mocking of feed URLs in integration tests — use local `.pb` fixture files

## Observability (SPEC.md §8) — mandatory, all three mechanisms

1. Dead man's switch — last step of successful `nightly.sh` only; URL from `$DEADMAN_URL` env var
2. node_exporter textfile metrics — `collector.prom` + `nightly.prom`; atomic write (tmp + rename)
   Nightly: stop_events_rows, ghost_trips_total, vp_arrivals_rows, vp_vehicles,
   stage durations (fetch_gtfs, stop_events, vp_arrivals, metrics, build, deploy)
3. Journald logs — both processes via systemd; one structured JSON summary line per nightly run

## Keeping docs current

After completing any bead or making a meaningful change, update these files as needed:

| File | When to update |
|------|---------------|
| `RECON.md` | If feed discoveries change known quirks or field population data |
| This file (commands, data flow, stack) | If new scripts are added, data flow changes, or tech changes |
| `README.md` | If setup steps or dev workflow changes |

Do not add story status, progress, or TODO sections to this file. That lives in beads.
Do not let commands drift from reality.

## v2 — remaining

Stop-level drill-down, excess-waiting-time metric, real-time site features, accounts/comments/API,
English version, ridership weighting.


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
