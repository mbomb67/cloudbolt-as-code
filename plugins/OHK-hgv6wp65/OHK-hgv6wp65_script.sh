#!/bin/sh
#
# Run Installer from File Archive (Linux)
#
# Runs an installer that File Archives (extensions/XUI-jbm5ac9e) keeps on
# CloudBolt. The archive header line at the end of this comment names the
# file. Run Task (server_actions/SVA-4sin3s0q) reads it and, before the task
# body starts, downloads the file to the VM, checks its SHA-256 and
# unpacks a zip or tar file. The body runs in that directory, which is also in
# CB_ARCHIVE_DIR.
#
# Installer is the installer's path inside the unpacked archive, for example
# app-7.4/install.sh. Leave it empty to run the downloaded file itself. By
# type: .rpm installs with rpm -U, .deb with dpkg -i, .sh runs with bash, and
# anything else runs as an executable, from its own directory. Installer
# arguments are split on spaces and passed to .sh files and executables.
#
# Built from Runbook Task Template (Linux), plugins/OHK-iub9a2jg: read its
# conventions before changing this script.
#
# cb-archive: {{ archive_file }} extract

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
ARCHIVE_FILE="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ archive_file }}{% endfilter %}{% endautoescape %}')"
INSTALLER="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ installer }}{% endfilter %}{% endautoescape %}')"
INSTALLER_ARGS="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ installer_args }}{% endfilter %}{% endautoescape %}')"

[ -n "${CB_ARCHIVE_DIR:-}" ] || fail "Run this task from Run Task: Run Task is what downloads the archive file."
case "$INSTALLER" in
    /*|*..*) fail "Installer must be a path inside the archive, without .. or a leading slash." ;;
esac
FILE_NAME="${ARCHIVE_FILE#*/}"
FILE_NAME="${FILE_NAME%@*}"
TARGET="$CB_ARCHIVE_DIR/${INSTALLER:-$FILE_NAME}"
if [ ! -f "$TARGET" ]; then
    fail "No file ${INSTALLER:-$FILE_NAME} in the archive. It holds: $(cd "$CB_ARCHIVE_DIR" && find . -maxdepth 2 | head -20 | tr '\n' ' ')"
fi
read -r -a ARGS <<< "$INSTALLER_ARGS"

# ---- task body ----
log "Running ${INSTALLER:-$FILE_NAME} from $ARCHIVE_FILE"
cd "$(dirname "$TARGET")"
rc=0
case "$TARGET" in
    *.rpm) $SUDO rpm -U --replacepkgs "$TARGET" ;;
    *.deb) $SUDO dpkg -i "$TARGET" ;;
    *.sh) $SUDO bash "$TARGET" ${ARGS[@]+"${ARGS[@]}"} ;;
    *) chmod +x "$TARGET" && $SUDO "$TARGET" ${ARGS[@]+"${ARGS[@]}"} ;;
esac || rc=$?
[ "$rc" -eq 0 ] || fail "The installer exited with code $rc."

log "Task finished."
task_output installer "${INSTALLER:-$FILE_NAME}"
task_output installer_exit_code "$rc"
CB_TASK
bash "$CB_TASK_FILE"
CB_TASK_RC=$?
rm -f "$CB_TASK_FILE"
exit "$CB_TASK_RC"
