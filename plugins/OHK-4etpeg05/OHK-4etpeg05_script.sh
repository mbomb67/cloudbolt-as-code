#!/bin/sh
#
# Create Linux App Account
#
# Step 2 of the Prepare Linux app host example workflow
# (runbook_workflows/SWF-69r5mmdb). Reads the application's name, account and
# home directory from the attached config file (runbook-examples/
# linux-app-host.conf in File Archives), creates the system group and a
# no-login system user when they do not exist, creates the home directory with
# bin, conf and logs below it, and publishes the account for the next step.
# Running it again changes nothing that is already in place.
#
# Built from the Runbook Task Template (Linux), plugins/OHK-iub9a2jg, which
# explains the input, template, output and privilege conventions.

umask 077
CB_TASK_FILE="$(mktemp)" || exit 1
cat > "$CB_TASK_FILE" <<'CB_TASK'
set -Eeuo pipefail

urldecode() { local v="${1//+/ }"; printf '%b' "${v//%/\\x}"; }
log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "ERROR: $*"; printf 'ERROR: %s\n' "$*" >&2; exit 1; }
task_output() {
    printf '%s' "$1" | grep -Eq '^[A-Za-z0-9_]+$' || fail "Output name '$1' must be letters, digits and underscores."
    printf 'CB_OUTPUT %s=%s\n' "$1" "$(printf '%s' "$2" | tr '\r\n' '  ')"
}
trap 'fail "line $LINENO: $BASH_COMMAND failed"' ERR
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo -n"; fi

# The last KEY=value line for a key in the config file, without surrounding
# quotes, or the default. Values are read as text and never run.
conf_get() {
    local line
    line="$(tr -d '\r' < "$CONFIG" | grep -E "^[[:space:]]*$1[[:space:]]*=" | tail -n 1 || true)"
    if [ -z "$line" ]; then printf '%s' "${2:-}"; return; fi
    printf '%s' "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/'
}
account_name() { printf '%s' "$2" | grep -Eq '^[a-z_][a-z0-9_-]{0,31}$' || fail "$1 in $CONFIG_FILE must be a Linux account name (lower case, up to 32 characters)."; }

# ---- inputs ----
CONFIG_FILE="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ config_file }}{% endfilter %}{% endautoescape %}')"
printf '%s' "$CONFIG_FILE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$' || fail "config_file must be a file name, such as linux-app-host.conf."

# ---- config file ----
CONFIG="${CB_ARCHIVE_DIR:-}/$CONFIG_FILE"
[ -n "${CB_ARCHIVE_DIR:-}" ] && [ -f "$CONFIG" ] || fail "$CONFIG_FILE is not attached to this step. Run the task from the Prepare Linux app host workflow, whose steps attach runbook-examples/$CONFIG_FILE from File Archives."
log "Reading $CONFIG_FILE (SHA-256 $(sha256sum "$CONFIG" | cut -c1-12))"
APP_NAME="$(conf_get APP_NAME demoapp)"
APP_USER="$(conf_get APP_USER "$APP_NAME")"
APP_GROUP="$(conf_get APP_GROUP "$APP_USER")"
APP_HOME="$(conf_get APP_HOME "/opt/$APP_NAME")"
account_name APP_NAME "$APP_NAME"
account_name APP_USER "$APP_USER"
account_name APP_GROUP "$APP_GROUP"
# Below a top-level directory (/opt/demoapp, /srv/demoapp), never a system path itself.
printf '%s' "$APP_HOME" | grep -Eq '^/[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$' || fail "APP_HOME in $CONFIG_FILE must be an absolute path at least two levels deep, such as /opt/demoapp."
case "$APP_HOME" in *..*) fail "APP_HOME in $CONFIG_FILE must not contain '..'." ;; esac

# ---- task body ----
ACCOUNT=existing
if getent group "$APP_GROUP" >/dev/null; then
    log "Group $APP_GROUP exists."
else
    $SUDO groupadd --system "$APP_GROUP"
    log "Created system group $APP_GROUP."
    ACCOUNT=created
fi
if id -u "$APP_USER" >/dev/null 2>&1; then
    log "User $APP_USER exists."
else
    NOLOGIN=/bin/false
    for candidate in /usr/sbin/nologin /sbin/nologin; do
        if [ -x "$candidate" ]; then NOLOGIN="$candidate"; break; fi
    done
    $SUDO useradd --system --gid "$APP_GROUP" --home-dir "$APP_HOME" --no-create-home \
        --shell "$NOLOGIN" --comment "$APP_NAME service account" "$APP_USER"
    log "Created system user $APP_USER (shell $NOLOGIN)."
    ACCOUNT=created
fi

for directory in "$APP_HOME" "$APP_HOME/bin" "$APP_HOME/conf" "$APP_HOME/logs"; do
    if [ -d "$directory" ]; then state="exists"; else state="created"; fi
    $SUDO install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$directory"
    log "$directory $state, owned by $APP_USER:$APP_GROUP, mode 0750."
done

log "Account $APP_USER ($ACCOUNT) and $APP_HOME are ready."
task_output app_user "$APP_USER"
task_output app_group "$APP_GROUP"
task_output app_uid "$(id -u "$APP_USER")"
task_output app_home "$APP_HOME"
task_output account "$ACCOUNT"
CB_TASK
bash "$CB_TASK_FILE"
CB_TASK_RC=$?
rm -f "$CB_TASK_FILE"
exit "$CB_TASK_RC"
