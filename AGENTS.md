# zakasnyava-li

Static website: Sofia public transit reliability from live GTFS/GTFS-RT feeds.
Two scheduled processes: collector (polls feeds every 20s) + nightly (03:10 Sofia). No backend.

## Commands

```bash
uv sync --dev && uv run pytest && uv run ruff check .
python pipeline/build_stop_events.py --date YYYY-MM-DD
python pipeline/build_vehicle_arrivals.py --date YYYY-MM-DD
python pipeline/compute_metrics.py --all-months --source merged
```

Site: `cd site && npm ci && npm run build`. Full list via `npm run` / `uv run ruff --help`.

## Rules

- **Source of record:** SPEC.md + RECON.md. Feed quirks are normative, do not improvise.
  Key: `stop_sequence`=0 → match `(trip_id, stop_id)`; 04:00 service day; `calendar_dates.txt` only.
- **Site:** Bulgarian UI, Chart.js bundled (not CDN), zero cookies/storage/external requests.
- **Branches:** epic work on `epic/N-name`, merge to main when done. Never commit epic to main directly.
- **Task tracking:** beads (`bd`) only. Never add TODO lists or status tables to this file.

## VP (v2)

VehiclePositions active since 2026-07. See RECON.md §7. `(trip_id, stop_id)` 100% populated,
same key as TU matcher. ~47% coverage → `--source merged` in nightly.

## Test conventions

`uv run pytest`. Fixtures in `tests/fixtures/`, golden in `tests/golden/`. SQLite in `:memory:`.

## Observability

SPEC.md §8. Dead man's switch, prom metrics, journald. Full schema in nightly.sh prom block.

## Docs

Update RECON.md for feed discoveries. This file for new scripts/data flow. README.md for setup changes.
Never put story progress here.

## v2 — remaining

Stop-level pages, excess-waiting-time, real-time features, accounts, English, ridership weighting.


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
