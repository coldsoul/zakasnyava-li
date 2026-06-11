# Закъснява ли?

Static website tracking Sofia public transit reliability from live GTFS/GTFS-RT feeds.

See `SPEC.md` for the full specification and `docs/internal/RECON.md` for confirmed feed facts.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
cd site && npm ci && npm run build
```

## Architecture

Two scheduled processes on a VPS:

- **`collector/collector.py`** — polls GTFS-RT feeds every 20s, writes compressed snapshots
- **`ops/nightly.sh`** — triggered at 03:10 Europe/Sofia by systemd timer; runs pipeline and deploys site

See `SPEC.md §5` for repository layout and `SPEC.md §8` for observability.
