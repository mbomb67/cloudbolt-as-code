#!/bin/sh
#
# Manage Linux Service
#
# Server task for Run Task (server_actions/SVA-4sin3s0q), built on the Server
# Task Template (Linux): shows, starts, stops or restarts one systemd unit and
# prints its state afterwards. A name without a suffix means NAME.service.
# Starting an active unit or stopping an inactive one changes nothing. Needs
# systemd, and root or passwordless sudo for start, stop and restart.
#
# References:
#   systemctl:            https://www.freedesktop.org/software/systemd/man/latest/systemctl.html
#   unit name characters: https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html

umask 077
CB_TASK_FILE="$(mktemp)" || exit 1
cat > "$CB_TASK_FILE" <<'CB_TASK'
set -Eeuo pipefail

urldecode() { local v="${1//+/ }"; printf '%b' "${v//%/\\x}"; }
log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "ERROR: $*"; printf 'ERROR: %s\n' "$*" >&2; exit 1; }
trap 'fail "line $LINENO: $BASH_COMMAND failed"' ERR
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo -n"; fi

# ---- inputs ----
SERVICE_NAME="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ service_name }}{% endfilter %}{% endautoescape %}')"
OPERATION="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ operation }}{% endfilter %}{% endautoescape %}' | tr '[:upper:]' '[:lower:]')"
printf '%s' "$SERVICE_NAME" | grep -Eq '^[A-Za-z0-9:_.@-]+$' \
    || fail "service_name '$SERVICE_NAME' must be one systemd unit name (letters, digits and : _ . @ -)."
case "$OPERATION" in
    status|start|stop|restart) ;;
    *) fail "operation must be status, start, stop or restart, not '$OPERATION'." ;;
esac

# ---- task body ----
command -v systemctl >/dev/null 2>&1 || fail "systemctl was not found; this task supports systemd only."
UNIT="$SERVICE_NAME"
case "$UNIT" in *.*) ;; *) UNIT="$UNIT.service" ;; esac

unit_property() { systemctl show -p "$1" "$UNIT" | cut -d= -f2-; }
[ "$(unit_property LoadState)" = "loaded" ] || fail "There is no unit named $UNIT on $(hostname)."

ACTIVE="$(unit_property ActiveState)"
log "$UNIT is $ACTIVE ($(unit_property SubState))."
case "$OPERATION" in
    start)
        if [ "$ACTIVE" = "active" ]; then log "Already active; nothing to do."
        else log "Starting..."; $SUDO systemctl start "$UNIT"; fi ;;
    stop)
        if [ "$ACTIVE" = "inactive" ]; then log "Already inactive; nothing to do."
        else log "Stopping..."; $SUDO systemctl stop "$UNIT"; fi ;;
    restart)
        log "Restarting..."; $SUDO systemctl restart "$UNIT" ;;
esac

ENABLED="$(systemctl is-enabled "$UNIT" 2>/dev/null || true)"
log "$UNIT is $(unit_property ActiveState) ($(unit_property SubState)), enabled: ${ENABLED:-unknown}."
log "Task finished."
CB_TASK
bash "$CB_TASK_FILE"
CB_TASK_RC=$?
rm -f "$CB_TASK_FILE"
exit "$CB_TASK_RC"
