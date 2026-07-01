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
cd site && npm ci                    # install site deps
cd site && npm run build             # site build (requires public/data/*.json)

# Pipeline entry-points
python pipeline/build_stop_events.py --date YYYY-MM-DD
python pipeline/build_vehicle_arrivals.py --date YYYY-MM-DD [--gtfs PATH]
python pipeline/compute_metrics.py --month YYYY-MM [--gtfs PATH] [--source tu|vp|merged]
python pipeline/compute_metrics.py --all-months [--gtfs PATH] [--source tu|vp|merged]
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

## v2 boundary — in progress

VehiclePositions matcher (VP-2 done), hybrid merge (VP-3 in progress).
Still out of scope: stop-level pages, excess-waiting-time metric, real-time site features, accounts, English version, third-party API.

## Development workflow

Each epic is implemented on a feature branch (`epic/N-short-name`), then a PR is opened for review before merging to `main`.

```bash
git checkout -b epic/1-collector   # start epic
# ... implement ...
gh pr create                        # open PR when epic is done
# wait for approval, then merge
```

Never commit directly to `main` for epic work.

## Keeping docs current

After completing any bead or making a meaningful change:

- **`AGENTS.md`** — mark completed beads `done`; update data flow or feed quirks if discoveries change them
- **`CLAUDE.md`** — update commands if new scripts are added; update stack table if tech changes
- **`README.md`** — update setup instructions if the dev workflow changes

Stale docs are bugs. If something here contradicts the code, fix the docs.

---

> Progressive discovery: this file covers the essentials. For pipeline internals read SPEC.md §2–§6. For site structure read SPEC.md §5–§6. For observability read SPEC.md §8. AGENTS.md has story status and data flow.


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
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
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
