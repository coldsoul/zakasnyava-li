# AGENTS.md — zakasnyava-li

Agent guide. Read CLAUDE.md first, then this file for story status and data flow.

## Branch and PR workflow

Each epic lives on its own feature branch. Never commit epic work directly to `main`.

```bash
git checkout -b epic/N-short-name   # one branch per epic
# implement all beads for the epic
gh pr create --title "epic(N): ..." # open PR when epic done
# wait for owner approval before merging
```

Branch naming: `epic/1-collector`, `epic/2-stop-events`, `epic/3-metrics`, `epic/4-site`, `epic/5-nightly`.

## Story status

| Bead | Description | Status |
|------|-------------|--------|
| 0.1 | Repo scaffold + CI | done |
| 1.1 | Collector + systemd unit | done |
| 2.1 | Static GTFS loader + DB schema | done |
| 2.2 | Snapshot streamer + stop event matcher | done |
| 3.1 | Metrics aggregation + grade engine + JSON output | done |
| 4.1 | Astro scaffold + methodology page | done |
| 4.2 | Homepage ranking table | done |
| 4.3 | Line page (charts, heatmap, weekly trend) | done |
| 5.1 | Nightly orchestration + observability | todo |
| 5.2 | VPS README + ops documentation | todo |

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
    → data/derived/stop_events.sqlite

  pipeline/compute_metrics.py --month YYYY-MM
    → site/public/data/index.json
    → site/public/data/line/{id}.json
    → site/public/data/feed_health.json

  astro build → site/dist/ → deployed static host
```

## Feed quirks — normative, implement exactly, do not "fix"

1. **No delay fields** — compute always: `delay = predicted_time − scheduled_time`
2. **`stop_sequence` always 0** — match on `(trip_id, stop_id)`; for loop routes pick scheduled stop whose time is closest to predicted_time
3. **`start_date` / `start_time` empty** — infer service date from `header.timestamp` (Europe/Sofia, 04:00 boundary)
4. **No `calendar.txt`** — service days from `calendar_dates.txt` only; expand active trip set BEFORE any ghost computation (mandatory)
5. **`schedule_relationship` = 0 always seen so far** — handle CANCELED (1) and SKIPPED (3) when they appear; ghost detection by absence
6. **stop_id formats match** between RT and static (e.g. `A2743`, `TM0300`) — confirmed 100% in recon

If feed contradicts RECON.md: stop and report, do not guess.

## Test conventions

- Framework: `pytest`; fixtures in `tests/fixtures/`; golden files in `tests/golden/`
- SQLite tests use in-memory `:memory:` — no real DB files in tests
- Bead 2.2 requires 8 specific fixture cases (see `SPEC.md §7 Story 2`)
- Bead 3.1 requires byte-identical golden-file tests (see `SPEC.md §7 Story 3`)
- No mocking of feed URLs in integration tests — use local `.pb` fixture files

## Observability (SPEC.md §8) — mandatory, all three mechanisms

1. Dead man's switch — last step of successful `nightly.sh` only; URL from `$DEADMAN_URL` env var
2. node_exporter textfile metrics — `collector.prom` + `nightly.prom`; always atomic write (tmp + rename)
3. Journald logs — both processes via systemd; one structured JSON summary line per nightly run

## Keeping docs current

After completing any bead or making a meaningful change, update these files as needed:

| File | When to update |
|------|---------------|
| `AGENTS.md` story status | Every completed bead — mark `done` |
| `AGENTS.md` data flow | If file paths, process names, or flow changes |
| `AGENTS.md` feed quirks | If RECON.md is updated with new discoveries |
| `CLAUDE.md` commands | If new entry-point scripts are added |
| `CLAUDE.md` stack table | If a dependency is swapped |
| `README.md` | If setup steps or dev workflow changes |

Do not leave completed beads marked `todo`. Do not let commands drift from reality.

## v2 — do not build

VehiclePositions processing, stop-level drill-down, excess-waiting-time metric, real-time site features, accounts/comments/API, English version, ridership weighting.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

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

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

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
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->
