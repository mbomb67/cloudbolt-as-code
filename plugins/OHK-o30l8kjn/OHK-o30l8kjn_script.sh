#!/bin/sh
#
# Configure Linux App Host
#
# Step 3 of the Prepare Linux app host example workflow
# (runbook_workflows/SWF-69r5mmdb). Takes the account and home directory from
# Create Linux App Account and the OS name from Check Linux App Host (the
# workflow passes their outputs in), reads the retention, environment and
# owner from the attached config file (runbook-examples/linux-app-host.conf in
# File Archives), and:
#   - writes a systemd-tmpfiles rule that deletes files in the logs directory
#     older than the retention period, and applies it;
#   - writes a host profile into the application's conf directory that records
#     the settings, the config file's checksum and the change ticket.
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
# Free text for the host profile: printable characters only, no quotes or
# shell expansion characters, at most 100 characters.
plain_text() { printf '%s' "$1" | tr -cd 'A-Za-z0-9 ._,:()/+-' | cut -c1-100; }

# ---- inputs ----
CONFIG_FILE="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ config_file }}{% endfilter %}{% endautoescape %}')"
APP_USER="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ app_user }}{% endfilter %}{% endautoescape %}')"
APP_HOME="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ app_home }}{% endfilter %}{% endautoescape %}')"
OS_NAME="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ os_name }}{% endfilter %}{% endautoescape %}')"
CHANGE_TICKET="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ change_ticket }}{% endfilter %}{% endautoescape %}')"
printf '%s' "$CONFIG_FILE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$' || fail "config_file must be a file name, such as linux-app-host.conf."
printf '%s' "$APP_USER" | grep -Eq '^[a-z_][a-z0-9_-]{0,31}$' || fail "app_user must be a Linux account name."
id -u "$APP_USER" >/dev/null 2>&1 || fail "User $APP_USER does not exist on $(hostname). Run Create Linux App Account first."
printf '%s' "$APP_HOME" | grep -Eq '^/[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$' || fail "app_home must be an absolute path at least two levels deep."
case "$APP_HOME" in *..*) fail "app_home must not contain '..'." ;; esac
[ -d "$APP_HOME/conf" ] && [ -d "$APP_HOME/logs" ] || fail "$APP_HOME/conf or $APP_HOME/logs is missing. Run Create Linux App Account first."
printf '%s' "$CHANGE_TICKET" | grep -Eq '^[A-Za-z0-9._-]{0,40}$' || fail "change_ticket must be up to 40 letters, digits, dots, dashes and underscores."
OS_NAME="$(plain_text "$OS_NAME")"

# ---- config file ----
CONFIG="${CB_ARCHIVE_DIR:-}/$CONFIG_FILE"
[ -n "${CB_ARCHIVE_DIR:-}" ] && [ -f "$CONFIG" ] || fail "$CONFIG_FILE is not attached to this step. Run the task from the Prepare Linux app host workflow, whose steps attach runbook-examples/$CONFIG_FILE from File Archives."
CONFIG_SHA256="$(sha256sum "$CONFIG" | cut -d' ' -f1)"
log "Reading $CONFIG_FILE (SHA-256 $(printf '%s' "$CONFIG_SHA256" | cut -c1-12))"
APP_NAME="$(conf_get APP_NAME demoapp)"
LOG_RETENTION_DAYS="$(conf_get LOG_RETENTION_DAYS 14)"
ENVIRONMENT="$(plain_text "$(conf_get ENVIRONMENT Development)")"
OWNER_TEAM="$(plain_text "$(conf_get OWNER_TEAM)")"
printf '%s' "$APP_NAME" | grep -Eq '^[a-z_][a-z0-9_-]{0,31}$' || fail "APP_NAME in $CONFIG_FILE must be lower case letters, digits, dashes and underscores."
printf '%s' "$LOG_RETENTION_DAYS" | grep -Eq '^[1-9][0-9]{0,3}$' || fail "LOG_RETENTION_DAYS in $CONFIG_FILE must be a whole number from 1 to 9999."
command -v systemd-tmpfiles >/dev/null 2>&1 || fail "systemd-tmpfiles is not installed on $(hostname)."

# ---- task body ----
APP_GROUP="$(id -gn "$APP_USER")"
WORK="$(mktemp -d)"

# 1. Log retention: systemd-tmpfiles keeps the logs directory and, on its daily
#    clean run, deletes files in it older than the retention period.
CLEANUP_RULE="/etc/tmpfiles.d/$APP_NAME.conf"
printf '# Written by the CloudBolt Runbooks workflow Prepare Linux app host.\nd %s/logs 0750 %s %s %sd\n' \
    "$APP_HOME" "$APP_USER" "$APP_GROUP" "$LOG_RETENTION_DAYS" > "$WORK/tmpfiles.conf"
$SUDO install -m 0644 "$WORK/tmpfiles.conf" "$CLEANUP_RULE"
$SUDO systemd-tmpfiles --create "$CLEANUP_RULE"
log "Wrote $CLEANUP_RULE: files in $APP_HOME/logs older than $LOG_RETENTION_DAYS days are deleted."

# 2. Host profile: what this workflow set up, from which config.
PROFILE="$APP_HOME/conf/host-profile.conf"
{
    printf '# Written by the CloudBolt Runbooks workflow Prepare Linux app host.\n'
    printf 'APP_NAME=%s\n' "$APP_NAME"
    printf 'APP_USER=%s\n' "$APP_USER"
    printf 'APP_HOME=%s\n' "$APP_HOME"
    printf 'ENVIRONMENT="%s"\n' "$ENVIRONMENT"
    printf 'OWNER_TEAM="%s"\n' "$OWNER_TEAM"
    printf 'OS_NAME="%s"\n' "$OS_NAME"
    printf 'LOG_RETENTION_DAYS=%s\n' "$LOG_RETENTION_DAYS"
    printf 'CHANGE_TICKET=%s\n' "$CHANGE_TICKET"
    printf 'CONFIG_FILE=%s\n' "$CONFIG_FILE"
    printf 'CONFIG_SHA256=%s\n' "$CONFIG_SHA256"
    printf 'CONFIGURED_AT=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$WORK/host-profile.conf"
$SUDO install -o "$APP_USER" -g "$APP_GROUP" -m 0640 "$WORK/host-profile.conf" "$PROFILE"
rm -rf "$WORK"
log "Wrote $PROFILE for $ENVIRONMENT${CHANGE_TICKET:+, change $CHANGE_TICKET}."

log "Host configured."
task_output cleanup_rule "$CLEANUP_RULE"
task_output profile_file "$PROFILE"
task_output log_retention_days "$LOG_RETENTION_DAYS"
CB_TASK
bash "$CB_TASK_FILE"
CB_TASK_RC=$?
rm -f "$CB_TASK_FILE"
exit "$CB_TASK_RC"
