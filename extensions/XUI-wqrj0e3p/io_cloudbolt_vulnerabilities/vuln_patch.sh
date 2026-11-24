# Linux updates, run as root on the target by the Vulnerabilities UI extension (vuln_patch.py).
#
# vuln_patch.py wraps this file in the same small POSIX sh launcher the scan uses (vuln_jobs._launcher),
# after a header of VAR=<quoted value> lines:
#   MODE            start | poll | probe | reboot | oldkernels | cleanup (the worker is started by "start")
#   RUN_ID          the run's id: letters, digits and dashes, 8 to 64 of them; it names the run folder
#   BOOT_ID         reboot mode only: the boot id the server had before the update (a UUID)
#   REBOOT_DELAY_S  reboot mode only: seconds to wait before the restart (a whole number)
# Nothing below is formatted with user input. Every mode exits 0 and reports through lines
# "CBVULN|<key>|<value>" on stdout; "CBVULN|done|1" is the last line of every call.
#
# One update is three calls from CloudBolt, because an update can outlive any one connection and can restart
# the server:
#   start    checks the distribution, copies this file to the run folder and starts it there in the
#            background (mode "worker"), so the update goes on if the connection drops.
#   worker   runs the package manager and keeps its state in $RUN/status, written to a temp file and moved
#            into place, so a poll never reads half a file. The status file survives a restart.
#   poll     prints that file and whether the worker is still alive. It is cheap: one cat and one kill -0.
# A restart is a separate call (reboot) and so is the removal of old kernels (oldkernels), which CloudBolt
# only asks for after the new boot id shows the server really restarted.
#
# Vendor documentation for every command used:
#   dnf command reference (upgrade, remove --oldinstallonly, --refresh, -y):
#     https://dnf.readthedocs.io/en/latest/command_ref.html
#   installonly_limit (how many kernels dnf keeps): https://dnf.readthedocs.io/en/latest/conf_ref.html
#   dnf needs-restarting -r (exit status 1 means a restart is required):
#     https://dnf-plugins-core.readthedocs.io/en/latest/needs_restarting.html
#   Amazon Linux 2023 and --releasever=latest:
#     https://docs.aws.amazon.com/linux/al2023/ug/deterministic-upgrades.html
#     https://docs.aws.amazon.com/linux/al2023/ug/managing-repos-os-updates.html
#   apt-get (update, dist-upgrade, autoremove, purge, -s simulate):
#     https://manpages.ubuntu.com/manpages/noble/man8/apt-get.8.html
#   systemd-run (transient service and timer units):
#     https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html

umask 077
set -u
export LC_ALL=C

MODE_REQ=${MODE-}
if [ -n "${1-}" ]; then
    MODE_REQ=$1
fi
RUN_ID=${RUN_ID-}
BOOT_ID=${BOOT_ID-}
REBOOT_DELAY_S=${REBOOT_DELAY_S-5}

emit() {
    local value=${2-}
    value=${value//$'\n'/ }
    value=${value//$'\r'/ }
    printf 'CBVULN|%s|%s\n' "$1" "$value"
}

# A value made safe for one status line: no separators or control characters, cut short.
san() {
    printf '%s' "$1" | tr '\n\r\t|' '    ' | cut -c1-"${2:-200}"
}

finish() {
    emit done 1
    exit 0
}

stop_with() {
    emit reason "$1"
    finish
}

valid_id() {
    case "$1" in
        ''|*[!A-Za-z0-9-]*) return 1 ;;
    esac
    [ "${#1}" -ge 8 ] && [ "${#1}" -le 64 ]
}

case "$MODE_REQ" in
    start|worker|poll|probe|reboot|oldkernels|cleanup) ;;
    *) stop_with "unknown mode" ;;
esac
if [ "$MODE_REQ" != probe ] && ! valid_id "$RUN_ID"; then
    stop_with "the run id has characters that are not allowed"
fi
RUN="/var/tmp/cbvuln-patch.$RUN_ID"
UNIT="cbvuln-patch-$RUN_ID"
BOOT_FILE=/proc/sys/kernel/random/boot_id

current_boot_id() {
    cat "$BOOT_FILE" 2>/dev/null | tr -d ' \n\r'
}

# ---- which distribution --------------------------------------------------
# DISTRO is rhel, ol, amzn2023 or ubuntu on the versions below; anything else is refused.
DISTRO=""
OS_NAME=""
detect() {
    local id ver major
    [ -r /etc/os-release ] || return 1
    id=$( . /etc/os-release; printf '%s' "${ID-}")
    ver=$( . /etc/os-release; printf '%s' "${VERSION_ID-}")
    OS_NAME=$( . /etc/os-release; printf '%s' "${PRETTY_NAME-}")
    case "$id" in
        rhel|ol)
            major=${ver%%.*}
            case "$major" in
                8|9|10) DISTRO=$id ;;
            esac ;;
        amzn)
            [ "$ver" = "2023" ] && DISTRO=amzn2023 ;;
        ubuntu)
            case "$ver" in
                22.04|24.04) DISTRO=ubuntu ;;
            esac ;;
    esac
    [ -n "$DISTRO" ]
}

is_rpm() {
    case "$DISTRO" in
        rhel|ol|amzn2023) return 0 ;;
    esac
    return 1
}

need_distro() {
    detect || stop_with "updates are not supported on this distribution (${OS_NAME:-unknown}); supported: RHEL and Oracle Linux 8-10, Amazon Linux 2023, Ubuntu 22.04 and 24.04"
    emit os "$OS_NAME"
    emit distro "$DISTRO"
    if [ "$(id -u)" != "0" ]; then
        stop_with "the update must run as root"
    fi
    if is_rpm; then
        command -v dnf >/dev/null 2>&1 || stop_with "dnf is not installed on the server"
    else
        command -v apt-get >/dev/null 2>&1 || stop_with "apt-get is not installed on the server"
    fi
}

# Names of the installed kernel packages on the dnf family, with their version in the form uname -r prints
# (version-release.arch), one "name version" pair per line. The names: kernel and kernel-core (Red Hat, Oracle
# Linux RHCK), kernel-uek and kernel-uek-core (Oracle Linux UEK), and kernel<major>.<minor> (Amazon Linux 2023
# names its kernel package after the series: kernel6.18 was observed on mb-dev, where `rpm -q kernel` finds
# nothing). The subpackages (-modules, -devel, ...) have longer names and are not listed.
rpm_kernels() {
    rpm -qa --qf '%{NAME} %{VERSION}-%{RELEASE}.%{ARCH}\n' 2>/dev/null \
        | awk '$1 ~ /^(kernel|kernel-core|kernel-uek|kernel-uek-core|kernel[0-9]+(\.[0-9]+)*)$/ {print $1, $2}'
}

# Names of the installed packages (state ii) on Ubuntu, one per line.
installed_debs() {
    dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' 2>/dev/null | awk '$1 == "ii" {print $2}'
}

# The ABI versions (6.8.0-1070-aws) of the kernel images in a package list on stdin, one per line.
deb_abis() {
    sed -nE 's/^linux-image-(unsigned-)?([0-9][A-Za-z0-9.+-]*)$/\2/p' | sort -u
}

# The newest installed kernel, in the form uname -r prints, or empty when it cannot be told (never a guess).
# The newest is the highest version of any kernel package; with two flavours installed (RHCK and UEK) it may
# not be the running flavour, which only makes the callers do less.
newest_kernel() {
    if is_rpm; then
        rpm_kernels | awk '{print $2}' | sort -V | tail -n 1
    else
        installed_debs | deb_abis | sort -V | tail -n 1
    fi
}

# Every installed package and its version, sorted, into the file $1 (a snapshot to count changes against).
# The dnf family: rpm name and epoch:version-release.arch; Ubuntu: dpkg binary package and version.
snapshot() {
    if is_rpm; then
        rpm -qa --qf '%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n' 2>/dev/null | sort >"$1"
    else
        dpkg-query -W -f='${binary:Package} ${Version}\n' 2>/dev/null | sort >"$1"
    fi
}

# Lines only in the second snapshot (new or changed packages), and lines only in the first (gone ones).
count_new() { comm -13 "$1" "$2" | wc -l | tr -d ' '; }
count_gone() { comm -23 "$1" "$2" | wc -l | tr -d ' '; }

# ---- the run folder --------------------------------------------------------
# /var/tmp is writable by every local user, so a folder is trusted only when it is a real directory (not a
# symlink), owned by the user this script runs as, with mode 700. Every mode that reads, writes, lists or removes
# a run folder checks it first, and none creates one with mkdir -p or removes one through a link.
safe_dir() {
    [ -d "$1" ] && [ ! -L "$1" ] && [ "$(stat -c '%u %a' "$1" 2>/dev/null)" = "$(id -u) 700" ]
}

# Stop with a reason when the run folder exists but cannot be trusted.
need_safe_run() {
    if { [ -e "$RUN" ] || [ -L "$RUN" ]; } && ! safe_dir "$RUN"; then
        stop_with "the run folder on the server is not safe to use (not a real directory owned by root with mode 700)"
    fi
}

# ---- a worker that is alive ----------------------------------------------
# pid_alive <run folder>: the pid in the folder belongs to a running worker.sh (a reused pid does not).
pid_alive() {
    local pid
    safe_dir "$1" || return 1
    [ -f "$1/pid" ] && [ ! -L "$1/pid" ] || return 1
    pid=$(cat "$1/pid" 2>/dev/null | tr -d ' \n\r')
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    kill -0 "$pid" 2>/dev/null && grep -qa 'worker\.sh' "/proc/$pid/cmdline" 2>/dev/null
}

# Another update on this server: a cbvuln-patch unit that is active, or a run folder with a live worker.
other_worker_running() {
    local dir
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        if systemctl list-units --all --no-legend --plain 'cbvuln-patch-*' 2>/dev/null \
            | awk '{print $3}' | grep -Eqx 'active|activating|reloading'; then
            return 0
        fi
    fi
    for dir in /var/tmp/cbvuln-patch.*; do
        safe_dir "$dir" || continue
        pid_alive "$dir" && return 0
    done
    return 1
}

# ---- status file ----------------------------------------------------------
STATE=running; PHASE=""; UPDATED=""; FAILED=""; EXITCODE=""; FAILURE=""
RESTART=""; RESTART_REASON=""; KERNEL_RUNNING=""; KERNEL_NEWEST=""

flush() {
    local tmp="$RUN/.status.$$"
    {
        emit state "$STATE"
        emit os "$OS_NAME"
        emit distro "$DISTRO"
        [ -z "$PHASE" ] || emit phase "$PHASE"
        [ -z "$UPDATED" ] || emit updated "$UPDATED"
        [ -z "$FAILED" ] || emit failed "$FAILED"
        [ -z "$EXITCODE" ] || emit exit "$EXITCODE"
        [ -z "$FAILURE" ] || emit failure "$(san "$FAILURE" 300)"
        [ -z "$RESTART" ] || emit restart "$RESTART"
        [ -z "$RESTART_REASON" ] || emit restart_reason "$(san "$RESTART_REASON" 200)"
        [ -z "$KERNEL_RUNNING" ] || emit kernel_running "$(san "$KERNEL_RUNNING" 100)"
        [ -z "$KERNEL_NEWEST" ] || emit kernel_newest "$(san "$KERNEL_NEWEST" 100)"
    } >"$tmp" 2>/dev/null && mv -f "$tmp" "$RUN/status" 2>/dev/null
    rm -f "$tmp" 2>/dev/null
}

# ---- mode: probe ----------------------------------------------------------
# One cheap read, before the update (the boot id to compare against) and while waiting for a restart.
mode_probe() {
    detect || true
    emit boot_id "$(current_boot_id)"
    emit kernel_running "$(san "$(uname -r)" 100)"
    emit kernel_newest "$(san "$(newest_kernel)" 100)"
    finish
}

# ---- mode: start ----------------------------------------------------------
mode_start() {
    local launched=0 tries
    need_distro
    if other_worker_running; then
        stop_with "another update is already running on this server"
    fi
    # Run folders an earlier job left behind (root-owned, older than three days) are removed first.
    find /var/tmp -maxdepth 1 -type d -name 'cbvuln-patch.*' -user root -perm 700 -mtime +3 -exec rm -rf {} + 2>/dev/null
    if [ -e "$RUN" ] || [ -L "$RUN" ]; then
        stop_with "this run folder already exists"
    fi
    # No -p: mkdir fails when the name is taken, by a link too, and never follows one.
    mkdir -m 700 "$RUN" 2>/dev/null || stop_with "the run folder could not be made in /var/tmp"
    safe_dir "$RUN" || stop_with "the run folder on the server is not safe to use"
    # The launcher deletes its own file when this call ends, so the worker runs from a copy. The copy has the
    # same header; the worker takes its mode from the first argument.
    if ! cp "$0" "$RUN/worker.sh" 2>/dev/null; then
        rm -rf "$RUN"
        stop_with "the update script could not be copied to the server"
    fi
    chmod 700 "$RUN/worker.sh"
    # A transient systemd service: not tied to this connection or its session, and listed by name.
    # https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html
    if command -v systemd-run >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        if systemd-run --quiet --unit="$UNIT" --collect bash "$RUN/worker.sh" worker >"$RUN/launch.out" 2>&1; then
            launched=1
        fi
    fi
    if [ "$launched" = 0 ]; then
        setsid nohup bash "$RUN/worker.sh" worker >"$RUN/launch.out" 2>&1 </dev/null &
    fi
    # The worker writes its status as its first act; wait for it to be sure it started.
    tries=0
    while [ "$tries" -lt 20 ]; do
        [ -s "$RUN/status" ] && break
        sleep 0.5
        tries=$((tries + 1))
    done
    if [ ! -s "$RUN/status" ]; then
        stop_with "the update did not start on the server"
    fi
    emit state running
    finish
}

# ---- mode: worker ---------------------------------------------------------
LOG=""
run_update() {
    # The count of what changed is taken from package lists before and after, not from the console text.
    snapshot "$RUN/before.pkgs"
    if is_rpm; then
        if [ "$DISTRO" = amzn2023 ]; then
            # Amazon Linux 2023 moves to the newest release with --releasever=latest:
            # https://docs.aws.amazon.com/linux/al2023/ug/deterministic-upgrades.html
            # https://docs.aws.amazon.com/linux/al2023/ug/managing-repos-os-updates.html
            # https://dnf.readthedocs.io/en/latest/command_ref.html
            dnf -y --refresh upgrade --releasever=latest >"$LOG" 2>&1
        else
            # https://dnf.readthedocs.io/en/latest/command_ref.html
            dnf -y --refresh upgrade >"$LOG" 2>&1
        fi
        EXITCODE=$?
    else
        # Non-interactive: no prompts about configuration files (keep the server's own), and services that
        # need a restart are restarted automatically. DPkg::Lock::Timeout waits for another package tool.
        # https://manpages.ubuntu.com/manpages/noble/man8/apt-get.8.html
        export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
        apt-get -o DPkg::Lock::Timeout=600 update >"$LOG" 2>&1
        EXITCODE=$?
        if [ "$EXITCODE" -eq 0 ]; then
            PHASE="Installing updates"
            flush
            apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
                -o DPkg::Lock::Timeout=600 dist-upgrade >>"$LOG" 2>&1
            EXITCODE=$?
        fi
    fi
    snapshot "$RUN/after.pkgs"
    UPDATED=$(count_new "$RUN/before.pkgs" "$RUN/after.pkgs")
    [ -n "$UPDATED" ] || UPDATED=0
}

# Whether the server must restart to run what was installed.
check_restart() {
    local out rc newest
    KERNEL_RUNNING=$(uname -r)
    KERNEL_NEWEST=$(newest_kernel)
    if is_rpm; then
        # Exit status 1 means a restart is required, 0 that it is not. dnf also exits 1 for a command it does not
        # know, so the exit status is trusted only after the command is shown to exist (its help works).
        # https://dnf-plugins-core.readthedocs.io/en/latest/needs_restarting.html
        rc=99
        if dnf needs-restarting -h >/dev/null 2>&1; then
            out=$(dnf needs-restarting -r 2>&1)
            rc=$?
        fi
        case "$rc" in
            0) RESTART=0 ;;
            1) RESTART=1; RESTART_REASON=$(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' | head -n 2 | tr '\n' ' ') ;;
            *)
                # The command is missing or failed: compare the newest installed kernel with the running one.
                newest=$KERNEL_NEWEST
                if [ -z "$newest" ]; then
                    RESTART=unknown; RESTART_REASON="dnf needs-restarting is not available and the kernel could not be compared"
                elif [ "$newest" != "$KERNEL_RUNNING" ]; then
                    RESTART=1; RESTART_REASON="a newer kernel is installed than the one running"
                else
                    RESTART=0
                fi ;;
        esac
    else
        if [ -f /var/run/reboot-required ]; then
            RESTART=1
            RESTART_REASON=$(head -n 3 /var/run/reboot-required.pkgs 2>/dev/null | tr '\n' ' ')
            [ -n "$RESTART_REASON" ] || RESTART_REASON="/var/run/reboot-required is present"
        else
            RESTART=0
        fi
    fi
}

mode_worker() {
    safe_dir "$RUN" || exit 0
    LOG="$RUN/update.log"
    echo $$ >"$RUN/pid"
    detect || true
    PHASE="Updating packages"
    flush
    run_update
    if [ "$EXITCODE" -ne 0 ]; then
        FAILED=$(grep -c -E '^(Error|E:)' "$LOG" 2>/dev/null)
        [ -n "$FAILED" ] && [ "$FAILED" -gt 0 ] 2>/dev/null || FAILED=1
        FAILURE=$(grep -E '^(Error|E:|Failed)' "$LOG" 2>/dev/null | tail -n 3 | tr '\n' ' ')
        [ -n "$FAILURE" ] || FAILURE=$(grep -v '^[[:space:]]*$' "$LOG" 2>/dev/null | tail -n 2 | tr '\n' ' ')
    else
        FAILED=0
    fi
    PHASE="Checking whether a restart is needed"
    flush
    check_restart
    PHASE="Finished"
    STATE=done
    flush
    exit 0
}

# ---- mode: poll -----------------------------------------------------------
mode_poll() {
    local first second
    emit boot_id "$(current_boot_id)"
    need_safe_run
    if [ ! -f "$RUN/status" ] || [ -L "$RUN/status" ]; then
        emit state missing
        finish
    fi
    first=$(cat "$RUN/status" 2>/dev/null)
    if printf '%s\n' "$first" | grep -qx 'CBVULN|state|running' && ! pid_alive "$RUN"; then
        # The worker may have finished between the read and the check: read again before calling it dead.
        second=$(cat "$RUN/status" 2>/dev/null)
        if printf '%s\n' "$second" | grep -qx 'CBVULN|state|running'; then
            printf '%s\n' "$second" | grep '^CBVULN|' | head -n 40
            emit state died
            finish
        fi
        first=$second
    fi
    printf '%s\n' "$first" | grep '^CBVULN|' | head -n 40
    finish
}

# ---- mode: reboot ---------------------------------------------------------
mode_reboot() {
    local cur
    printf '%s' "$BOOT_ID" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' \
        || stop_with "the boot id is not valid"
    case "$REBOOT_DELAY_S" in
        ''|*[!0-9]*) stop_with "the restart delay is not valid" ;;
    esac
    [ "$REBOOT_DELAY_S" -le 60 ] || stop_with "the restart delay is not valid"
    cur=$(current_boot_id)
    emit boot_id "$cur"
    if [ "$cur" != "$BOOT_ID" ]; then
        # The server already restarted since the update began: a second restart would be for nothing.
        emit note "the server restarted before the restart was requested"
        finish
    fi
    # Detached with a delay, so this call can return before the connection goes away.
    # https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html
    if command -v systemd-run >/dev/null 2>&1 && [ -d /run/systemd/system ] \
        && systemd-run --quiet --unit="cbvuln-reboot-$RUN_ID" --on-active="$REBOOT_DELAY_S" --collect systemctl reboot >/dev/null 2>&1; then
        emit note "restart scheduled"
        finish
    fi
    setsid nohup sh -c 'sleep "$1"; systemctl reboot || reboot || shutdown -r now' sh "$REBOOT_DELAY_S" >/dev/null 2>&1 </dev/null &
    emit note "restart scheduled"
    finish
}

# ---- mode: oldkernels -----------------------------------------------------
# Names of the packages a simulated apt run (on stdin) would remove, one per line.
removals() {
    awk '$1 == "Remv" || $1 == "Purg" {print $2}' | sort -u
}

# The release part of a kernel ABI version without the flavour: 6.8.0-1035-aws -> 6.8.0-1035.
kernel_base() {
    printf '%s' "$1" | sed -nE 's/^([0-9]+\.[0-9]+\.[0-9]+-[0-9]+).*/\1/p'
}

# The packages of the old kernels, one per line. $1 is the running kernel, stdin the installed package names.
# An old kernel is an installed linux-image (or linux-image-unsigned) whose ABI differs from the running one. Its
# packages are the installed ones whose name starts with linux- and holds -<base> followed by the end of the name
# or a dash (image, modules, modules-extra, headers, tools, cloud-tools, and the flavour-prefixed forms such as
# linux-aws-6.8-tools-6.8.0-1035). A name that holds the running kernel's base is never selected, and a
# metapackage (linux-image-aws, no version in its name) never matches.
select_old_debs() {
    local names abi base runbase bases=""
    names=$(cat)
    runbase=$(kernel_base "$1")
    [ -n "$runbase" ] || return 0
    for abi in $(printf '%s\n' "$names" | deb_abis); do
        [ "$abi" = "$1" ] && continue
        base=$(kernel_base "$abi")
        # The same release in another flavour is left alone.
        [ -n "$base" ] && [ "$base" != "$runbase" ] && bases="$bases $base"
    done
    [ -n "$bases" ] || return 0
    printf '%s\n' "$names" | awk -v bases="$bases" -v run="$runbase" '
        function has(name, base,   pat, rest, pos, nxt) {
            pat = "-" base
            rest = name
            while ((pos = index(rest, pat)) > 0) {
                nxt = substr(rest, pos + length(pat), 1)
                if (nxt == "" || nxt == "-") return 1
                rest = substr(rest, pos + 1)
            }
            return 0
        }
        BEGIN { n = split(bases, list, " ") }
        index($0, "linux-") == 1 && !has($0, run) {
            for (i = 1; i <= n; i++) if (has($0, list[i])) { print $0; break }
        }' | sort -u
}

# Ubuntu: purge the packages of the old kernels, after the checks. Sets OLD_COUNT and OLD_RC.
old_kernels_deb() {
    local names extra
    local -a list
    names=$(installed_debs | select_old_debs "$(uname -r)")
    if [ -z "$names" ]; then
        emit note "no old kernels are installed"
        return 0
    fi
    mapfile -t list <<<"$names"
    # apt-get -s only simulates. Anything it would remove besides the selected packages stops the cleanup.
    # https://manpages.ubuntu.com/manpages/noble/man8/apt-get.8.html
    extra=$(comm -13 <(printf '%s\n' "${list[@]}" | sort -u) <(apt-get -s purge "${list[@]}" 2>/dev/null | removals) | head -n 1)
    if [ -n "$extra" ]; then
        emit note "old kernels were left in place: purging them would also remove $(san "$extra" 100)"
        return 0
    fi
    snapshot "$RUN/kernels-before.pkgs"
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
    apt-get -y -o DPkg::Lock::Timeout=600 purge "${list[@]}" >"$RUN/kernels.log" 2>&1
    OLD_RC=$?
    snapshot "$RUN/kernels-after.pkgs"
    OLD_COUNT=$(count_gone "$RUN/kernels-before.pkgs" "$RUN/kernels-after.pkgs")
}

# The dnf family. remove --oldinstallonly removes old installonly packages (kernels), keeping only the latest
# versions and the running kernel, whatever installonly_limit says; with nothing old it stops with "No old
# installonly packages found for removal" and a non-zero status. Sets OLD_COUNT and OLD_RC.
# https://dnf.readthedocs.io/en/latest/command_ref.html (remove --oldinstallonly)
old_kernels_rpm() {
    snapshot "$RUN/kernels-before.pkgs"
    dnf -y remove --oldinstallonly >"$RUN/kernels.log" 2>&1
    OLD_RC=$?
    snapshot "$RUN/kernels-after.pkgs"
    OLD_COUNT=$(count_gone "$RUN/kernels-before.pkgs" "$RUN/kernels-after.pkgs")
    if [ "$OLD_RC" -ne 0 ] && [ "$OLD_COUNT" = 0 ] \
        && grep -qiE 'No old installonly packages|No packages marked for removal|Nothing to do' "$RUN/kernels.log" 2>/dev/null; then
        # Nothing old to remove is not a failure, whatever status dnf gives it.
        OLD_RC=0
        emit note "no old kernels are installed"
    elif [ "$OLD_RC" -ne 0 ]; then
        emit note "dnf could not remove the old kernels (exit code $OLD_RC)"
        OLD_RC=0
    fi
}

mode_oldkernels() {
    local running newest
    need_distro
    need_safe_run
    if [ ! -e "$RUN" ]; then
        mkdir -m 700 "$RUN" 2>/dev/null || stop_with "the run folder could not be made in /var/tmp"
        safe_dir "$RUN" || stop_with "the run folder on the server is not safe to use"
    fi
    OLD_COUNT=0
    OLD_RC=0
    running=$(uname -r)
    newest=$(newest_kernel)
    # Only a server running its newest kernel is cleaned: the running kernel and the one before it are what
    # a failed boot would fall back to.
    if [ -z "$newest" ]; then
        emit note "old kernels were left in place: the newest installed kernel could not be told"
    elif [ "$running" != "$newest" ]; then
        emit note "old kernels were left in place: the server is not running its newest kernel"
    elif is_rpm; then
        old_kernels_rpm
    else
        old_kernels_deb
    fi
    emit removed "$OLD_COUNT"
    emit exit "$OLD_RC"
    if [ "$OLD_RC" -ne 0 ]; then
        emit failure "$(san "$(grep -E '^(Error|E:|Failed)' "$RUN/kernels.log" 2>/dev/null | tail -n 2 | tr '\n' ' ')" 300)"
    fi
    emit kernel_running "$(san "$running" 100)"
    emit kernel_newest "$(san "$(newest_kernel)" 100)"
    finish
}

# ---- mode: cleanup --------------------------------------------------------
mode_cleanup() {
    if [ ! -e "$RUN" ] && [ ! -L "$RUN" ]; then
        finish
    fi
    need_safe_run
    if pid_alive "$RUN"; then
        emit note "the update is still running, so its folder was left in place"
        finish
    fi
    rm -rf "$RUN" 2>/dev/null
    finish
}

# ---- dispatch ---------------------------------------------------------------
case "$MODE_REQ" in
    probe) mode_probe ;;
    start) mode_start ;;
    worker) mode_worker ;;
    poll) mode_poll ;;
    reboot) mode_reboot ;;
    oldkernels) mode_oldkernels ;;
    cleanup) mode_cleanup ;;
esac
finish
