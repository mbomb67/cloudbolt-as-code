# Software list collection, run as root on the target by the Vulnerabilities UI extension.
#
# vuln_jobs.py wraps this file in a small POSIX sh launcher that writes it to a
# temp file and runs it with bash, after a header of VAR=<quoted value> lines:
#   UP_URL, UP_TOKEN        where to upload the software list, and the token for it
#   UP_INSECURE (0|1)       1 = skip the TLS check when talking to CloudBolt
#   TOOL_URL_AMD64, TOOL_SHA_AMD64, TOOL_GH_AMD64   the Syft tarball for x86_64: CloudBolt's
#   TOOL_URL_ARM64, TOOL_SHA_ARM64, TOOL_GH_ARM64   URL, its sha256, and the GitHub URL
#   ALLOW_GITHUB (0|1)      1 = fall back to GitHub when CloudBolt cannot be reached
#   HOLD_DIR                a directory to leave the file in for CloudBolt to collect, or empty
#   EXTRA_EXCLUDES          newline-separated absolute paths to skip
#   SCAN_TIMEOUT_S          seconds the inventory tool may run before it is stopped
# Nothing below is formatted with user input. The script always exits 0 and
# reports through lines "CBVULN|<key>|<value>" on stdout. Stdout stays small
# because cloud agent channels keep only the first or the last few KB: Syft's
# own output goes to a file and one compact "summary" line carries everything
# the job needs. It is printed at the start of the final block and again as
# the last line, so whichever end of the output survives has it.
#
# Syft options used (names checked against the vendor documentation):
#   https://oss.anchore.com/docs/reference/syft/configuration/
#   https://oss.anchore.com/docs/guides/sbom/catalogers/
# curl reads the URL and token from its config on stdin (-K -), so they never
# show in the process list:
#   https://curl.se/docs/manpage.html

umask 077
set -u
START_TS=$(date +%s)

S_OS=""; S_ARCH=""; S_TOOL=""; S_BYTES=""; S_UPLOAD=""; S_REASON=""

emit() {
    local value=${2-}
    value=${value//$'\n'/ }
    value=${value//$'\r'/ }
    printf 'CBVULN|%s|%s\n' "$1" "$value"
}

# A value made safe for the summary line: no separators or control characters, cut short.
san() {
    printf '%s' "$1" | tr '\n\r\t~|' '     ' | cut -c1-"${2:-200}"
}

summary() {
    local line="os=$(san "$S_OS" 120)~arch=$S_ARCH~tool=$S_TOOL~bytes=$S_BYTES"
    line="$line~upload=$S_UPLOAD~seconds=$(( $(date +%s) - START_TS ))~reason=$(san "$S_REASON" 300)~final=$1"
    emit summary "$line"
}

stop_with() {
    S_REASON=$1
    [ -n "$S_UPLOAD" ] || S_UPLOAD=failed
    emit reason "$1"
    finish
}

finish() {
    emit seconds "$(( $(date +%s) - START_TS ))"
    summary 1
    emit done 1
    summary 1
    exit 0
}

# ---- OS identity ---------------------------------------------------------
if [ -r /etc/os-release ]; then
    S_OS=$( . /etc/os-release; printf '%s' "${PRETTY_NAME-}")
fi
emit os "$S_OS"

# ---- what this server needs ----------------------------------------------
case "$(uname -m)" in
    x86_64|amd64)  S_ARCH=amd64; TOOL_URL=${TOOL_URL_AMD64-}; TOOL_SHA=${TOOL_SHA_AMD64-}; TOOL_GH=${TOOL_GH_AMD64-} ;;
    aarch64|arm64) S_ARCH=arm64; TOOL_URL=${TOOL_URL_ARM64-}; TOOL_SHA=${TOOL_SHA_ARM64-}; TOOL_GH=${TOOL_GH_ARM64-} ;;
    *) S_ARCH=$(uname -m); emit arch "$S_ARCH"; stop_with "This server's processor type ($(uname -m)) is not supported" ;;
esac
emit arch "$S_ARCH"

for cmd in curl tar gzip sha256sum; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        stop_with "$cmd is not installed on the server"
    fi
done

# The work folder must allow programs to run: the inventory tool is run from it,
# and CIS-hardened servers mount /var/tmp noexec. Each base is tried in turn and
# proved by running a two-line script from it. A folder an earlier run left
# behind (root-owned, older than six hours) is removed first.
WORK=""; WORK_BASE=""
pick_work() {
    local base pattern template dir probe
    for base in /var/tmp /root /opt; do
        [ -d "$base" ] || continue
        if [ "$base" = "/root" ]; then
            pattern='.cbvuln.*'; template="$base/.cbvuln.XXXXXX"
        else
            pattern='cbvuln.*'; template="$base/cbvuln.XXXXXX"
        fi
        find "$base" -maxdepth 1 -type d -name "$pattern" -user root -mmin +360 -exec rm -rf {} + 2>/dev/null
        dir=$(mktemp -d "$template" 2>/dev/null) || continue
        [ -d "$dir" ] || continue
        probe="$dir/.exec-test"
        if printf '#!/bin/sh\nexit 0\n' >"$probe" 2>/dev/null && chmod 700 "$probe" 2>/dev/null \
            && "$probe" >/dev/null 2>&1; then
            rm -f "$probe"
            WORK=$dir; WORK_BASE=$base
            return 0
        fi
        rm -rf "$dir"
    done
    return 1
}
if ! pick_work; then
    stop_with "No folder that allows programs to run was found (tried /var/tmp, /root and /opt)"
fi
trap 'rm -rf "$WORK"' EXIT

# The tool is small, but the software list of a busy server can be tens of MB
# before it is compressed: stop early rather than fill the disk.
FREE_KB=$(df -Pk "$WORK" 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "$FREE_KB" ] && [ "$FREE_KB" -lt 524288 ] 2>/dev/null; then
    stop_with "Less than 512 MB is free in $WORK_BASE"
fi

# Quote a value for a curl config file line.
cfg_quote() {
    local v=$1
    v=${v//\\/\\\\}
    v=${v//\"/\\\"}
    printf '"%s"' "$v"
}

# A curl error line made into something a person can act on.
curl_reason() {
    case "$1" in
        "curl: (60)"*)
            printf '%s' "the server does not trust the certificate of the CloudBolt address (curl 60); install the issuing CA on the server, or turn on Skip certificate check in the Vulnerability Settings" ;;
        *) printf '%s' "$1" ;;
    esac
}

# ---- get Syft -------------------------------------------------------------
# From CloudBolt first (the token goes in a header), then, if allowed, from
# GitHub. The sha256 is checked either way: a file that does not match is never run.
TGZ="$WORK/syft.tar.gz"
got=0
DL_ERR=""
if [ -n "$TOOL_URL" ]; then
    # --proto: only http and https, whatever a redirect or a mistyped address says.
    flags=(-sS -f --retry 2 --max-time 600 --proto '=https,http')
    [ "${UP_INSECURE-0}" = "1" ] && flags+=(-k)
    if {
        printf 'url = %s\n' "$(cfg_quote "$TOOL_URL")"
        printf 'output = %s\n' "$(cfg_quote "$TGZ")"
        printf 'header = %s\n' "$(cfg_quote "X-Vuln-Token: ${UP_TOKEN-}")"
    } | curl "${flags[@]}" -K - >/dev/null 2>"$WORK/curl.err"; then
        got=1; S_TOOL=appliance
    else
        reason=$(grep -m 1 '^curl: (' "$WORK/curl.err" | cut -c1-200)
        DL_ERR=$(curl_reason "$reason")
        emit note "Could not download the tool from CloudBolt: ${reason:-no reason given}"
        rm -f "$TGZ"
    fi
fi
if [ "$got" = "0" ] && [ "${ALLOW_GITHUB-0}" = "1" ] && [ -n "$TOOL_GH" ]; then
    # A public URL, so it is fine on the command line.
    if curl -sS -f -L --retry 2 --max-time 600 --proto '=https' --proto-redir '=https' -o "$TGZ" "$TOOL_GH" \
        >/dev/null 2>"$WORK/curl.err"; then
        got=1; S_TOOL=github
    else
        reason=$(grep -m 1 '^curl: (' "$WORK/curl.err" | cut -c1-200)
        DL_ERR=$(curl_reason "$reason")
        emit note "Could not download the tool from GitHub: ${reason:-no reason given}"
        rm -f "$TGZ"
    fi
fi
if [ "$got" = "0" ]; then
    stop_with "The inventory tool could not be downloaded from CloudBolt or GitHub${DL_ERR:+: $DL_ERR}"
fi
emit tool_source "$S_TOOL"

GOT_SHA=$(sha256sum "$TGZ" | cut -d' ' -f1)
if [ -z "$TOOL_SHA" ] || [ "$GOT_SHA" != "$TOOL_SHA" ]; then
    stop_with "The downloaded inventory tool does not match its published checksum, so it was not run"
fi
# Only the syft binary comes out of the tarball.
if ! tar -xzf "$TGZ" -C "$WORK" --no-same-owner syft 2>"$WORK/tar.err" || [ ! -f "$WORK/syft" ]; then
    stop_with "The inventory tool could not be unpacked"
fi
chmod 700 "$WORK/syft"
rm -f "$TGZ"

# ---- what to skip ----------------------------------------------------------
# Syft wants every pattern for a directory scan to start with ./ . Each path
# is given twice: the directory itself and everything below it.
EXCL=()
add_exclude() {
    local rel=${1#/}
    [ -n "$rel" ] || return 0
    EXCL+=(--exclude "./$rel" --exclude "./$rel/**")
}
add_exclude "$WORK"
for p in /proc /sys /dev /run /snap /var/lib/docker /var/lib/containers /var/lib/containerd /var/lib/kubelet \
    /var/lib/rancher; do
    add_exclude "$p"
done
while IFS= read -r p; do
    [ -n "$p" ] && add_exclude "$p"
done <<<"${EXTRA_EXCLUDES-}"

# Network file systems can hold terabytes that belong to another server, and a
# hung mount would stall the run, so each remote mount point is skipped.
# /proc/mounts writes a space in a path as \040 (and a tab, newline or
# backslash the same way); those are skipped without trying to decode them.
skipped_escaped=0
remote_mounts=0
if [ -r /proc/mounts ]; then
    while read -r _dev mp fstype _rest; do
        case "$fstype" in
            # fuse.* is every FUSE file system with a subtype (sshfs, s3fs, rclone, gcsfuse, blobfuse);
            # fuseblk, a local disk, has no dot and does not match.
            nfs|nfs4|cifs|smb3|smbfs|ceph|glusterfs|lustre|9p|afs|autofs|gpfs|beegfs|davfs|ncpfs|fuse.*) ;;
            *) continue ;;
        esac
        [ "$mp" = "/" ] && continue
        case "$mp" in
            # Kernel file systems mounted inside these are never read; counting them would
            # report network mounts on a server that has none.
            /proc/*|/sys/*|/dev/*) continue ;;
        esac
        case "$mp" in
            *\\*) skipped_escaped=$((skipped_escaped + 1)); continue ;;
        esac
        add_exclude "$mp"
        remote_mounts=$((remote_mounts + 1))
    done </proc/mounts
fi
[ "$remote_mounts" -gt 0 ] && emit note "Skipped $remote_mounts network or automounted folder(s)"
[ "$skipped_escaped" -gt 0 ] && emit note "Could not skip $skipped_escaped network or automounted folder(s) whose path has a space or special character; they were scanned"

# ---- build the software list ----------------------------------------------
# SYFT_FILE_METADATA_SELECTION=none drops the per-file listing the matcher never
# reads (the file is much smaller and faster to make). The kernel cataloger is
# turned off because it produced mostly duplicate entries in testing.
# Low CPU and disk priority, so the server's own work comes first.
PREFIX=(nice -n 10)
command -v ionice >/dev/null 2>&1 && PREFIX=(ionice -c3 nice -n 10)
# A scan that cannot finish must not run on until the job gives up: timeout stops it
# (and kills it 30 seconds later if it ignores the stop).
TCMD=()
if command -v timeout >/dev/null 2>&1 && [[ "${SCAN_TIMEOUT_S-}" =~ ^[0-9]+$ ]]; then
    TCMD=(timeout --kill-after=30 "$SCAN_TIMEOUT_S")
fi
export SYFT_CHECK_FOR_APP_UPDATE=false
export SYFT_FILE_METADATA_SELECTION=none
export XDG_CACHE_HOME="$WORK/cache"
cd / || stop_with "Could not enter the root directory"
"${PREFIX[@]}" ${TCMD[@]+"${TCMD[@]}"} "$WORK/syft" dir:/ ${EXCL[@]+"${EXCL[@]}"} \
    --select-catalogers '-linux-kernel-cataloger' \
    -o "syft-json=$WORK/sbom.json" -q >"$WORK/syft.out" 2>"$WORK/syft.err"
RC=$?
if [ "${#TCMD[@]}" -gt 0 ] && { [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; }; then
    stop_with "The software list took longer than $(( SCAN_TIMEOUT_S / 60 )) minutes and was stopped"
fi
if [ "$RC" -ne 0 ] || [ ! -s "$WORK/sbom.json" ]; then
    stop_with "The inventory tool exited with $RC: $(tail -n 3 "$WORK/syft.err" 2>/dev/null | tr '\n' ' ' | cut -c1-200)"
fi
if ! gzip -c "$WORK/sbom.json" >"$WORK/sbom.json.gz" 2>/dev/null || [ ! -s "$WORK/sbom.json.gz" ]; then
    stop_with "The software list could not be compressed"
fi
rm -f "$WORK/sbom.json"
S_BYTES=$(wc -c <"$WORK/sbom.json.gz" | tr -d ' ')
emit sbom_bytes "$S_BYTES"

# ---- hand it over ------------------------------------------------------------
# A server that cannot reach CloudBolt gets HOLD_DIR and no upload URL: the file
# waits in a root-only directory, and CloudBolt reads it over the channel it
# ran this script through and then removes the directory. One a job that died
# left behind is removed by the next scan.
if [ -n "${HOLD_DIR-}" ]; then
    find "${HOLD_DIR%/*}" -maxdepth 1 -type d -name 'vuln-hold-*' -mmin +180 -exec rm -rf {} + 2>/dev/null
    rm -rf "$HOLD_DIR"
    if mkdir -m 700 "$HOLD_DIR" 2>/dev/null && mv "$WORK/sbom.json.gz" "$HOLD_DIR/sbom.json.gz"; then
        S_UPLOAD=held
        emit held 1
        emit upload held
        finish
    fi
    emit held 0
    stop_with "The server could not keep the software list for CloudBolt to collect"
fi

if [ -z "${UP_URL-}" ]; then
    stop_with "No upload address was given"
fi
flags=(-sS -f --retry 2 --max-time 600 --proto '=https,http')
[ "${UP_INSECURE-0}" = "1" ] && flags+=(-k)
{
    printf 'url = %s\n' "$(cfg_quote "$UP_URL")"
    printf 'upload-file = %s\n' "$(cfg_quote "$WORK/sbom.json.gz")"
    printf 'header = %s\n' "$(cfg_quote "X-Vuln-Token: ${UP_TOKEN-}")"
} | curl "${flags[@]}" -K - >/dev/null 2>"$WORK/curl.err"
CRC=${PIPESTATUS[1]}
if [ "$CRC" -eq 0 ]; then
    S_UPLOAD=ok
    emit upload ok
else
    S_UPLOAD=failed
    reason=$(grep -m 1 '^curl: (' "$WORK/curl.err" | cut -c1-200)
    case "$reason" in
        "curl: (60)"*)
            reason="the server does not trust the certificate of the CloudBolt address (curl 60); install the issuing CA on the server, or turn on Skip certificate check in the Vulnerability Settings" ;;
        "") reason=$(head -n 1 "$WORK/curl.err" | cut -c1-200) ;;
    esac
    emit upload failed
    stop_with "The software list could not be uploaded (curl exit $CRC): ${reason:-upload failed}"
fi

finish
