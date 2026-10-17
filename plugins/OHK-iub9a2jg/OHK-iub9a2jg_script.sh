#!/bin/sh
#
# Runbook Task Template (Linux)
#
# Base script for Linux tasks. Run Task (server_actions/SVA-4sin3s0q)
# lists every Remote Script that fits the VM, so a copy of this script with
# the Linux OS family set shows up there without a new button. Copy it, rename
# it, declare your inputs, and replace the task body between the CB_TASK
# markers. This copy prints a short summary of the VM and an optional
# message.
#
# Conventions:
#   Shell    This outer part is plain sh. It writes the task body to a private
#            temporary file and runs it with bash, because Azure Run Command can
#            run the file with sh (dash on Ubuntu and Debian). Write the body in
#            bash.
#   Inputs   Each declared input is read once, under "inputs", inside single
#            quotes, wrapped in Django's autoescape-off and urlencode filter
#            tags, and decoded with urldecode. The encoded text is only
#            letters, digits and percent signs, so no value can end the string
#            or the CB_TASK block. Keep the variable inside the tags bare (no
#            filter after its name): CloudBolt creates the script's inputs from
#            the bare template variables in the file and deletes the others.
#            Validate every input before using it.
#   Template CloudBolt renders the whole file as a Django template first, so
#            never write template braces anywhere else, even in a comment, and
#            avoid a brace followed by a hash or percent sign (bash string
#            length is one; use wc -m instead).
#   Output   log writes to stdout. fail prints an ERROR line, writes it to
#            stderr too, and exits 1. Every transport reads the exit code.
#   Outputs  task_output name value prints a CB_OUTPUT name=value line. Run
#            Task turns those lines into the job's outputs, and a later
#            workflow step can read them. Print them last: WinRM drops stdout
#            when a script fails, and Azure Run Command keeps only the last
#            4 KB of output.
#   Root     Azure Run Command and AWS SSM run as root. Over SSH or VMware
#            Tools the script runs as the Remote Script's credentials, else the
#            server's, else the username and password Run Task asks for; prefix
#            privileged commands with $SUDO (passwordless sudo), or tick Run
#            with sudo on the Remote Script (SSH only).

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

# ---- inputs ----
MESSAGE="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ message }}{% endfilter %}{% endautoescape %}')"
[ "$(printf '%s' "$MESSAGE" | wc -m)" -le 200 ] || fail "message must be 200 characters or fewer."

# ---- task body ----
log "Host: $(hostname)"
if [ -r /etc/os-release ]; then
    log "OS: $(. /etc/os-release && printf '%s' "${PRETTY_NAME:-unknown}")"
fi
log "Kernel: $(uname -r)"
log "Up since: $(uptime -s 2>/dev/null || echo unknown)"
log "Running as: $(id -un)"
if [ -n "$MESSAGE" ]; then log "Message: $MESSAGE"; fi

log "Task finished."
task_output kernel "$(uname -r)"
task_output host_name "$(hostname)"
CB_TASK
bash "$CB_TASK_FILE"
CB_TASK_RC=$?
rm -f "$CB_TASK_FILE"
exit "$CB_TASK_RC"
