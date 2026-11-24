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

# The newest installed kernel, in the form uname -r prints, or empty.
newest_kernel() {
    if is_rpm; then
        rpm -q kernel --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' 2>/dev/null | grep -E '^[0-9]' | sort -V | tail -n 1
    else
        dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' 'linux-image-[0-9]*' 2>/dev/null \
            | awk '$1 == "ii" {print $2}' | sed 's/^linux-image-//' | sort -V | tail -n 1
    fi
}

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
        UPDATED=$(rpm -qa --qf '%{INSTALLTIME}\n' 2>/dev/null | awk -v s="$START_TS" '$1 >= s' | wc -l | tr -d ' ')
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
        UPDATED=$(grep -c '^Setting up ' "$LOG" 2>/dev/null)
    fi
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
    START_TS=$(date +%s)
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
# The kernel packages the Ubuntu cleanup may purge: image, headers, modules, modules-extra and tools of a numbered
# release, nothing else. Used to filter apt's own list and to check apt's simulation of the purge.
KERNEL_RE='^linux-(image|headers|modules|modules-extra|tools)-[0-9][A-Za-z0-9.+-]*$'

# Names of the packages a simulated apt run (on stdin) would remove, one per line.
removals() {
    awk '$1 == "Remv" || $1 == "Purg" {print $2}' | sort -u
}

mode_oldkernels() {
    local before after rc names count extras
    local -a list
    need_distro
    need_safe_run
    if [ ! -e "$RUN" ]; then
        mkdir -m 700 "$RUN" 2>/dev/null || stop_with "the run folder could not be made in /var/tmp"
        safe_dir "$RUN" || stop_with "the run folder on the server is not safe to use"
    fi
    EXITCODE=0
    if is_rpm; then
        # Removes installonly packages (kernels) beyond installonly_limit (default 3), never the running kernel.
        # https://dnf.readthedocs.io/en/latest/command_ref.html
        # https://dnf.readthedocs.io/en/latest/conf_ref.html
        before=$(rpm -qa 2>/dev/null | wc -l | tr -d ' ')
        dnf -y remove --oldinstallonly >"$RUN/kernels.log" 2>&1
        rc=$?
        after=$(rpm -qa 2>/dev/null | wc -l | tr -d ' ')
        count=$((before - after))
        [ "$count" -ge 0 ] || count=0
    else
        # Not a blanket autoremove: simulate it, keep only the kernel packages it would remove, purge those.
        # https://manpages.ubuntu.com/manpages/noble/man8/apt-get.8.html
        export DEBIAN_FRONTEND=noninteractive
        names=$(apt-get -s autoremove --purge 2>/dev/null | removals | grep -E "$KERNEL_RE")
        rc=0
        count=0
        if [ -n "$names" ]; then
            mapfile -t list <<<"$names"
            # Purging a kernel can pull other packages with it; if the simulation shows anything that is not a
            # kernel package, nothing is removed.
            extras=$(apt-get -s purge "${list[@]}" 2>/dev/null | removals | grep -Ev "$KERNEL_RE")
            if [ -n "$extras" ]; then
                emit note "old kernels were left in place: purging them would also remove other packages"
            else
                apt-get -y -o DPkg::Lock::Timeout=600 purge "${list[@]}" >"$RUN/kernels.log" 2>&1
                rc=$?
                [ "$rc" -eq 0 ] && count=${#list[@]}
            fi
        fi
    fi
    emit removed "$count"
    emit exit "$rc"
    if [ "$rc" -ne 0 ]; then
        emit failure "$(san "$(grep -E '^(Error|E:|Failed)' "$RUN/kernels.log" 2>/dev/null | tail -n 2 | tr '\n' ' ')" 300)"
    fi
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
