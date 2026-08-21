#!/bin/sh
set -u

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STATE_ROOT=${AGENT_TELEMETRY_STATE_DIR:-${XDG_STATE_HOME:-${HOME:?}/.local/state}/agent-telemetry}
LOG_PATH=$STATE_ROOT/collect.log
LOCK_PATH=$STATE_ROOT/collect.lock
MODE=${1:-refresh}
TRIGGER=${2:-manual}

mkdir -p "$STATE_ROOT"

if [ "${AGENT_TELEMETRY_LOCKED:-0}" != "1" ]; then
    if [ -f "$LOG_PATH" ] && [ "$(wc -c < "$LOG_PATH")" -ge 1048576 ]; then
        mv -f "$LOG_PATH" "$LOG_PATH.1"
    fi
    exec >>"$LOG_PATH" 2>&1
    printf '%s mode=%s trigger=%s start\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$TRIGGER"
    case "$TRIGGER" in
        windows-task-*)
            /usr/bin/nice -n 10 /usr/bin/ionice -c 3 python3 "$PROJECT_ROOT/stability.py" --lock-run "$LOCK_PATH" -- "$0" "$MODE" "$TRIGGER"
            ;;
        *)
            python3 "$PROJECT_ROOT/stability.py" --lock-run "$LOCK_PATH" -- "$0" "$MODE" "$TRIGGER"
            ;;
    esac
    RESULT=$?
    if [ "$RESULT" -eq 75 ]; then
        case "$TRIGGER" in
            reboot|windows-task-*)
                printf '%s mode=%s trigger=%s finish exit=0\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$TRIGGER"
                printf '%s trigger=%s state=lock_busy_noop\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$TRIGGER"
                RESULT=0
                ;;
        esac
    fi
    # The lock supervisor has returned, so this bounded network outcome check
    # cannot hold the collection lock or be killed as an orphaned subprocess.
    if [ -f "$STATE_ROOT/pages-check-request.json" ]; then
        if ! python3 "$PROJECT_ROOT/collect.py" --check-pages; then
            printf '%s pages_check=degraded\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        fi
    fi
    exit "$RESULT"
fi

case "$MODE" in
    refresh|catchup)
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
        RESULT=$?
        printf '%s mode=%s trigger=%s finish exit=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$TRIGGER" "$RESULT"
        exit "$RESULT"
        ;;
    *)
        printf '%s invalid_mode=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE"
        exit 64
        ;;
esac

case "$TRIGGER" in
    windows-task-*)
        if [ "$PUBLISH" -eq 0 ] && python3 "$PROJECT_ROOT/stability.py" --state-root "$STATE_ROOT" --fresh-within-minutes 20; then
            printf '%s trigger=%s state=fresh_noop\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$TRIGGER"
            printf '%s mode=%s trigger=%s finish exit=0\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$TRIGGER"
            exit 0
        fi
        ;;
esac

# Refresh Claude's normalized quota cache once inside the collection lock.  A
# failed quota check is observable but never blocks the underlying collection.
if ! python3 "$PROJECT_ROOT/collect.py" --capture-claude-usage; then
    printf '%s claude_usage_capture=degraded\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi

RESULT=0
if [ "$PUBLISH" -eq 1 ]; then
    if ! python3 "$PROJECT_ROOT/collect.py"; then
        python3 "$PROJECT_ROOT/collect.py" --record-publish failure --publish-reason collect_failed
        RESULT=2
    elif ! python3 "$PROJECT_ROOT/collect.py" --scrub; then
        python3 "$PROJECT_ROOT/collect.py" --record-publish blocked --publish-reason scrub_gate
        RESULT=3
    elif ! python3 "$PROJECT_ROOT/collect.py" --commit-existing; then
        python3 "$PROJECT_ROOT/collect.py" --record-publish failure --publish-reason collect_commit_failed
        RESULT=4
    elif ! python3 "$PROJECT_ROOT/publish.py" --repo "$PROJECT_ROOT" --state-root "$STATE_ROOT"; then
        RESULT=5
    fi
else
    if ! python3 "$PROJECT_ROOT/collect.py"; then
        RESULT=2
    fi
fi

printf '%s mode=%s trigger=%s finish exit=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$TRIGGER" "$RESULT"
exit "$RESULT"
