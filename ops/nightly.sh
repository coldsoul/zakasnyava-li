#!/usr/bin/env bash
# Nightly pipeline: fetch GTFS → stop events → metrics → site → deploy.
# Triggered by nightly.timer at 03:10 Europe/Sofia.
# On success: ping dead man's switch (last step). On failure: exit nonzero.
set -euo pipefail

export TZ="Europe/Sofia"

# ── Configuration (override via EnvironmentFile in nightly.service) ──────────

export REPO_DIR="${REPO_DIR:-/opt/zakasnyava-li}"
export DATA_ROOT="${DATA_ROOT:-/var/lib/zakasnyava-li/data}"
export DB_PATH="${DB_PATH:-$DATA_ROOT/derived/stop_events.sqlite}"
export SITE_DATA_DIR="${SITE_DATA_DIR:-$REPO_DIR/site/public/data}"
export PROM_DIR="${PROM_DIR:-/var/lib/node_exporter/textfile_collector}"
export GH_PAGES_REMOTE="${GH_PAGES_REMOTE:-}"   # overridable; defaults to repo's origin
export DEADMAN_URL="${DEADMAN_URL:-}"
export PYTHON="${PYTHON:-$REPO_DIR/.venv/bin/python}"
export NPM="${NPM:-npm}"

# ── State ────────────────────────────────────────────────────────────────────

RUN_TS=$(date +%s)
TODAY=$(date +%Y-%m-%d)
YESTERDAY=$(date -d yesterday +%Y-%m-%d)
MONTH=$(date +%Y-%m)

declare -A T_START
declare -A T_DUR

stage_start() { T_START[$1]=$(date +%s); }
stage_end()   { T_DUR[$1]=$(( $(date +%s) - T_START[$1] )); }

EXIT_STATUS=1
LINES_GRADED=0
STOP_ROWS=0
GHOST_COUNT=0

# Preserve previous success timestamp across failures
PREV_SUCCESS_TS=0
if [[ -f "$PROM_DIR/nightly.prom" ]]; then
    PREV_SUCCESS_TS=$(awk '/^nightly_last_success_timestamp_seconds /{print $2}' \
        "$PROM_DIR/nightly.prom" 2>/dev/null || echo 0)
fi

# ── Observability helpers ─────────────────────────────────────────────────────

write_prom() {
    [[ -d "$PROM_DIR" ]] || return 0
    local success_ts=$PREV_SUCCESS_TS
    [[ $EXIT_STATUS -eq 0 ]] && success_ts=$(date +%s)
    local tmp
    tmp=$(mktemp "$PROM_DIR/.nightly.prom.XXXXXX")
    cat > "$tmp" <<PROM
# HELP nightly_last_run_timestamp_seconds Unix timestamp of last nightly run start
# TYPE nightly_last_run_timestamp_seconds gauge
nightly_last_run_timestamp_seconds $RUN_TS
# HELP nightly_last_success_timestamp_seconds Unix timestamp of last successful nightly run
# TYPE nightly_last_success_timestamp_seconds gauge
nightly_last_success_timestamp_seconds $success_ts
# HELP nightly_last_exit_status Exit status of last nightly run (0=success)
# TYPE nightly_last_exit_status gauge
nightly_last_exit_status $EXIT_STATUS
# HELP nightly_duration_seconds Duration of each pipeline stage in seconds
# TYPE nightly_duration_seconds gauge
nightly_duration_seconds{stage="fetch_gtfs"} ${T_DUR[fetch_gtfs]:-0}
nightly_duration_seconds{stage="stop_events"} ${T_DUR[stop_events]:-0}
nightly_duration_seconds{stage="vp_arrivals"} ${T_DUR[vp_arrivals]:-0}
nightly_duration_seconds{stage="metrics"} ${T_DUR[metrics]:-0}
nightly_duration_seconds{stage="build"} ${T_DUR[build]:-0}
nightly_duration_seconds{stage="deploy"} ${T_DUR[deploy]:-0}
# HELP nightly_stop_events_rows Stop events rows processed in last run
# TYPE nightly_stop_events_rows gauge
nightly_stop_events_rows $STOP_ROWS
# HELP nightly_ghost_trips_total Ghost trips counted in last run
# TYPE nightly_ghost_trips_total gauge
nightly_ghost_trips_total $GHOST_COUNT
# HELP nightly_vp_arrivals_rows VehiclePosition arrival rows processed in last run
# TYPE nightly_vp_arrivals_rows gauge
nightly_vp_arrivals_rows $VP_ROWS
# HELP nightly_vp_vehicles VehiclePosition distinct vehicles observed
# TYPE nightly_vp_vehicles gauge
nightly_vp_vehicles $VP_VEHICLES
# HELP nightly_lines_graded Lines graded in last run
# TYPE nightly_lines_graded gauge
nightly_lines_graded $LINES_GRADED
PROM
    mv -f "$tmp" "$PROM_DIR/nightly.prom"
}

log_summary() {
    local now; now=$(date +%s)
    local dur=$(( now - RUN_TS ))
    local fg=${T_DUR[fetch_gtfs]:-0}
    local se=${T_DUR[stop_events]:-0}
    local vp=${T_DUR[vp_arrivals]:-0}
    local me=${T_DUR[metrics]:-0}
    local bu=${T_DUR[build]:-0}
    local de=${T_DUR[deploy]:-0}
    # Emit one structured JSON line to stderr → journald
    "${PYTHON}" - 1>&2 <<PY
import json
print(json.dumps({
    "event": "nightly.run",
    "date": "${TODAY}",
    "month": "${MONTH}",
    "exit_status": ${EXIT_STATUS},
    "duration_sec": ${dur},
    "stage_durations": {
        "fetch_gtfs":  ${fg},
        "stop_events": ${se},
        "vp_arrivals": ${vp},
        "metrics":     ${me},
        "build":       ${bu},
        "deploy":      ${de}
    },
    "stop_events_rows": ${STOP_ROWS},
    "ghost_trips_total": ${GHOST_COUNT},
    "vp_arrivals_rows": ${VP_ROWS},
    "vp_vehicles": ${VP_VEHICLES},
    "lines_graded": ${LINES_GRADED}
}))
PY
}

cleanup() {
    EXIT_STATUS=$?  # capture before any other command overwrites it
    write_prom
    log_summary
}
trap cleanup EXIT

# ── Stage 0: Pull latest code ────────────────────────────────────────────────

git -C "${REPO_DIR}" pull --ff-only

# ── Stage 1: Fetch static GTFS ───────────────────────────────────────────────

stage_start fetch_gtfs
"${PYTHON}" "${REPO_DIR}/collector/fetch_static_gtfs.py"
stage_end fetch_gtfs

# ── Stage 2: Build yesterday's stop events ───────────────────────────────────

stage_start stop_events
DB_PATH="${DB_PATH}" "${PYTHON}" "${REPO_DIR}/pipeline/build_stop_events.py" \
    --date "${YESTERDAY}"

STOP_ROWS=$(DB="${DB_PATH}" DT="${YESTERDAY}" "${PYTHON}" -c "
import sqlite3, os
con = sqlite3.connect(os.environ['DB'])
print(con.execute(
    'SELECT COUNT(*) FROM stop_events WHERE service_date=?', (os.environ['DT'],)
).fetchone()[0])
" 2>/dev/null || echo 0)

GHOST_COUNT=$(DB="${DB_PATH}" DT="${YESTERDAY}" "${PYTHON}" -c "
import sqlite3, os
con = sqlite3.connect(os.environ['DB'])
print(con.execute(
    'SELECT COUNT(DISTINCT trip_id) FROM stop_events WHERE service_date=? AND is_ghost=1',
    (os.environ['DT'],)
).fetchone()[0])
" 2>/dev/null || echo 0)

stage_end stop_events

# ── Stage 2b: Build yesterday's vehicle arrivals (GPS ground truth) ──────────

stage_start vp_arrivals
DB_PATH="${DB_PATH}" "${PYTHON}" "${REPO_DIR}/pipeline/build_vehicle_arrivals.py" \
    --date "${YESTERDAY}"

VP_ROWS=$(DB="${DB_PATH}" DT="${YESTERDAY}" "${PYTHON}" -c "
import sqlite3, os
con = sqlite3.connect(os.environ['DB'])
print(con.execute(
    'SELECT COUNT(*) FROM vehicle_arrivals WHERE service_date=?', (os.environ['DT'],)
).fetchone()[0])
" 2>/dev/null || echo 0)

VP_VEHICLES=$(DB="${DB_PATH}" DT="${YESTERDAY}" "${PYTHON}" -c "
import sqlite3, os
con = sqlite3.connect(os.environ['DB'])
print(con.execute(
    'SELECT COUNT(DISTINCT vehicle_id) FROM vehicle_arrivals WHERE service_date=? AND vehicle_id IS NOT NULL',
    (os.environ['DT'],)
).fetchone()[0])
" 2>/dev/null || echo 0)

stage_end vp_arrivals

# ── Stage 3: Compute current-month metrics (VP+TU merged) ─────────────────────

stage_start metrics
RESULT=$(SITE_DATA_DIR="${SITE_DATA_DIR}" DB_PATH="${DB_PATH}" \
    "${PYTHON}" "${REPO_DIR}/pipeline/compute_metrics.py" --all-months --source merged)
LINES_GRADED=$("${PYTHON}" -c "
import json, sys
d = json.loads(sys.argv[1])
print(d.get('by_month', {}).get('${MONTH}', 0))
" "${RESULT}")
stage_end metrics

# ── Stage 4: Build static site ───────────────────────────────────────────────

stage_start build
(cd "${REPO_DIR}/site" && "${NPM}" run build)
stage_end build

# ── Stage 5: Deploy to GitHub Pages ──────────────────────────────────────────

_deploy_gh_pages() {
    local remote work
    remote="${GH_PAGES_REMOTE:-$(git -C "${REPO_DIR}" remote get-url origin)}"
    work=$(mktemp -d)
    trap "rm -rf '${work}'" RETURN

    cp -a "${REPO_DIR}/site/dist/." "${work}/"
    touch "${work}/.nojekyll"

    git -C "${work}" init -b gh-pages
    git -C "${work}" config user.email "nightly@zakasnyava-li.local"
    git -C "${work}" config user.name "nightly"
    git -C "${work}" add -A
    git -C "${work}" commit -m "deploy ${TODAY}"
    git -C "${work}" push --force "${remote}" HEAD:gh-pages
}

stage_start deploy
_deploy_gh_pages
stage_end deploy

EXIT_STATUS=0

# Dead man's switch — success only, very last step
if [[ -n "${DEADMAN_URL}" ]]; then
    curl -fsS --max-time 10 --retry 3 --retry-delay 5 "${DEADMAN_URL}" || true
fi
