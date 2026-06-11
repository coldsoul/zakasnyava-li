# zakasnyava-li

Static website: Sofia public transit reliability from live GTFS/GTFS-RT feeds.
Two scheduled processes: `collector` (polls feeds every 20s) + `nightly` (builds + deploys site at 03:10 Sofia). No backend.

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
cd site && npm ci && npm run build   # site build
```

## Source of record — read before touching pipeline code

- **`SPEC.md`** — normative spec; all §4 definitions exact; do not improvise alternatives
- **`docs/internal/RECON.md`** — confirmed empirical facts about the live feeds

Feed quirks are in `SPEC.md §2`. They are normative — the matcher MUST handle them exactly. If the live feed contradicts RECON.md at implementation time, stop and report; do not guess.

## Before touching site code

- Bulgarian UI text; templates in `site/src/templates/`
- Chart.js bundled locally (not CDN) — site must work with JS disabled except charts
- `methodology.astro` text matches `SPEC.md §4` verbatim — do not paraphrase
- Zero cookies, zero localStorage, no external requests

## v2 boundary — do NOT build

VehiclePositions processing, stop-level pages, excess-waiting-time metric, real-time site features, accounts, English version, third-party API.

---

> Progressive discovery: this file covers the essentials. For pipeline internals read SPEC.md §2–§6. For site structure read SPEC.md §5–§6. For observability read SPEC.md §8. AGENTS.md has story status and data flow.
