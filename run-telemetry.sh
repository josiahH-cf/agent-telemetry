#!/bin/sh
set -u

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STATE_ROOT=${AGENT_TELEMETRY_STATE_DIR:-${XDG_STATE_HOME:-${HOME:?}/.local/state}/agent-telemetry}
LOG_PATH=$STATE_ROOT/collect.log
LOCK_PATH=$STATE_ROOT/collect.lock
MODE=${1:-refresh}

mkdir -p "$STATE_ROOT"
if [ -f "$LOG_PATH" ] && [ "$(wc -c < "$LOG_PATH")" -ge 1048576 ]; then
    mv -f "$LOG_PATH" "$LOG_PATH.1"
fi
exec >>"$LOG_PATH" 2>&1
printf '%s mode=%s start\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE"

exec 9>"$LOCK_PATH"
if ! flock -n 9; then
    printf '%s lock=busy\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit 75
fi

case "$MODE" in
    refresh)
        if python3 "$PROJECT_ROOT/collect.py" --publish-due >/dev/null 2>&1; then
            PUBLISH=1
        else
            PUBLISH=0
        fi
        ;;
    publish)
        PUBLISH=1
        ;;
    lock-probe)
        python3 "$PROJECT_ROOT/collect.py" --check
        exit $?
        ;;
    *)
        printf '%s invalid_mode=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE"
        exit 64
        ;;
esac

if [ "$PUBLISH" -eq 1 ]; then
    if ! python3 "$PROJECT_ROOT/collect.py"; then
        python3 "$PROJECT_ROOT/collect.py" --record-publish failure --publish-reason collect_failed
        exit 0
    fi
    if ! python3 "$PROJECT_ROOT/collect.py" --scrub; then
        python3 "$PROJECT_ROOT/collect.py" --record-publish blocked --publish-reason scrub_gate
        python3 "$PROJECT_ROOT/collect.py" --commit || true
        exit 0
    fi
    if ! git -C "$PROJECT_ROOT" push --dry-run origin main; then
        python3 "$PROJECT_ROOT/collect.py" --record-publish failure --publish-reason git_push_dry_run_failed
        python3 "$PROJECT_ROOT/collect.py" --commit || true
        exit 0
    fi
    python3 "$PROJECT_ROOT/collect.py" --record-publish success --publish-reason scheduled_push
    if ! python3 "$PROJECT_ROOT/collect.py" --commit; then
        python3 "$PROJECT_ROOT/collect.py" --record-publish failure --publish-reason collect_commit_failed
        exit 0
    fi
    if ! python3 "$PROJECT_ROOT/collect.py" --scrub; then
        python3 "$PROJECT_ROOT/collect.py" --record-publish blocked --publish-reason scrub_gate
        python3 "$PROJECT_ROOT/collect.py" --commit || true
        exit 0
    fi
    if git -C "$PROJECT_ROOT" push origin main; then
        python3 "$PROJECT_ROOT/collect.py" --record-publish success --publish-reason pushed
    else
        python3 "$PROJECT_ROOT/collect.py" --record-publish failure --publish-reason git_push_failed
        python3 "$PROJECT_ROOT/collect.py" --commit || true
    fi
else
    python3 "$PROJECT_ROOT/collect.py"
fi
printf '%s mode=%s finish\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE"
