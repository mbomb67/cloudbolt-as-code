#!/bin/sh
#
# Check Linux App Host
#
# Step 1 of the Prepare Linux app host example workflow
# (runbook_workflows/SWF-69r5mmdb). The workflow step attaches the config file
# runbook-examples/linux-app-host.conf from File Archives; this task reads its
# limits from that file, checks free disk space, memory and required commands,
# fails when a check does not pass, and publishes what it found for the later
# steps.
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

# The last KEY=value line for a key in the config file, without surrounding
# quotes, or the default. Values are read as text and never run.
conf_get() {
    local line
    line="$(tr -d '\r' < "$CONFIG" | grep -E "^[[:space:]]*$1[[:space:]]*=" | tail -n 1 || true)"
    if [ -z "$line" ]; then printf '%s' "${2:-}"; return; fi
    printf '%s' "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/'
}
whole_number() { printf '%s' "$2" | grep -Eq '^[0-9]{1,7}$' || fail "$1 in $CONFIG_FILE must be a whole number."; }

# ---- inputs ----
CONFIG_FILE="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ config_file }}{% endfilter %}{% endautoescape %}')"
printf '%s' "$CONFIG_FILE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$' || fail "config_file must be a file name, such as linux-app-host.conf."

# ---- config file ----
CONFIG="${CB_ARCHIVE_DIR:-}/$CONFIG_FILE"
[ -n "${CB_ARCHIVE_DIR:-}" ] && [ -f "$CONFIG" ] || fail "$CONFIG_FILE is not attached to this step. Run the task from the Prepare Linux app host workflow, whose steps attach runbook-examples/$CONFIG_FILE from File Archives."
log "Reading $CONFIG_FILE (SHA-256 $(sha256sum "$CONFIG" | cut -c1-12))"
MIN_FREE_GB="$(conf_get MIN_FREE_GB 2)"
MIN_MEMORY_MB="$(conf_get MIN_MEMORY_MB 900)"
DATA_MOUNT="$(conf_get DATA_MOUNT /)"
REQUIRED_COMMANDS="$(conf_get REQUIRED_COMMANDS 'tar gzip systemctl')"
whole_number MIN_FREE_GB "$MIN_FREE_GB"
whole_number MIN_MEMORY_MB "$MIN_MEMORY_MB"
printf '%s' "$DATA_MOUNT" | grep -Eq '^/[A-Za-z0-9._/-]*$' || fail "DATA_MOUNT in $CONFIG_FILE must be an absolute path."
[ -d "$DATA_MOUNT" ] || fail "DATA_MOUNT $DATA_MOUNT does not exist on $(hostname)."
printf '%s' "$REQUIRED_COMMANDS" | grep -Eq '^[A-Za-z0-9._+ -]*$' || fail "REQUIRED_COMMANDS in $CONFIG_FILE must be command names separated by spaces."

# ---- task body ----
OS_NAME="$(uname -s)"
if [ -r /etc/os-release ]; then OS_NAME="$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-$OS_NAME}")"; fi
FREE_GB=$(( $(df -Pk "$DATA_MOUNT" | awk 'NR == 2 { print $4 }') / 1048576 ))
MEMORY_MB=$(( $(awk '/^MemTotal:/ { print $2 }' /proc/meminfo) / 1024 ))
PACKAGE_MANAGER=none
for candidate in dnf yum apt-get zypper; do
    if command -v "$candidate" >/dev/null 2>&1; then PACKAGE_MANAGER="$candidate"; break; fi
done
log "OS: $OS_NAME; package manager: $PACKAGE_MANAGER"

PROBLEMS=""
if [ "$FREE_GB" -ge "$MIN_FREE_GB" ]; then
    log "Free space on $DATA_MOUNT: $FREE_GB GB (at least $MIN_FREE_GB GB): ok"
else
    PROBLEMS="$PROBLEMS $DATA_MOUNT has $FREE_GB GB free, below MIN_FREE_GB $MIN_FREE_GB."
fi
if [ "$MEMORY_MB" -ge "$MIN_MEMORY_MB" ]; then
    log "Memory: $MEMORY_MB MB (at least $MIN_MEMORY_MB MB): ok"
else
    PROBLEMS="$PROBLEMS The VM has $MEMORY_MB MB of memory, below MIN_MEMORY_MB $MIN_MEMORY_MB."
fi
for command_name in $REQUIRED_COMMANDS; do
    if command -v "$command_name" >/dev/null 2>&1; then
        log "Command $command_name: ok"
    else
        PROBLEMS="$PROBLEMS Command $command_name is not installed."
    fi
done
[ -z "$PROBLEMS" ] || fail "$(hostname) is not ready:$PROBLEMS"

log "All checks passed."
task_output os_name "$OS_NAME"
task_output free_gb "$FREE_GB"
task_output memory_mb "$MEMORY_MB"
task_output package_manager "$PACKAGE_MANAGER"
CB_TASK
bash "$CB_TASK_FILE"
CB_TASK_RC=$?
rm -f "$CB_TASK_FILE"
exit "$CB_TASK_RC"
