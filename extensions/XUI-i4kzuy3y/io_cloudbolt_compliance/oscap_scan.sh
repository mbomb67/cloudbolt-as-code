# OpenSCAP scan, run as root on the target by the OpenSCAP UI extension.
#
# oscap_jobs.py wraps this file in a small POSIX sh launcher that writes it to
# a temp file and runs it with bash, after a header of VAR=<quoted value> lines:
#   MODE (scan|check), DATASTREAM, PROFILE, DEFAULT_PROFILES, ALLOW_INSTALL (0|1),
#   UP_REPORT, UP_RESULTS, UP_ARF (upload URLs or empty), UP_TOKEN (local
#   backend only), UP_INSECURE (0|1), HOLD_DIR (a directory to leave the result
#   files in for CloudBolt to collect, or empty).
# Nothing below is formatted with user input. The script always exits 0 and
# reports through lines "OSCAP|<key>|<value>" on stdout. Stdout stays small
# because cloud agent channels keep only the first or the last few KB:
# oscap's own per-rule output goes to a file, scan mode prints no datastream
# or profile lists, and one compact "summary" line carries everything the
# panel needs. It is printed right after the scan and again as the last line
# before "done", so whichever end of the output survives has it.

umask 077
START_TS=$(date +%s)

# Values of the summary line.
S_READY=0; S_VERSION=""; S_OS=""; S_EXIT=""; S_KV=""; S_SCORE=""
S_DS=""; S_PROFILE=""; S_TITLE=""; S_PROBLEM=""
S_ST_REPORT=""; S_ST_RESULTS=""; S_ST_ARF=""

emit() {
    local value=${2-}
    value=${value//$'\n'/ }
    value=${value//$'\r'/ }
    printf 'OSCAP|%s|%s\n' "$1" "$value"
}

# A value made safe for the summary line: no separators or control characters, cut short.
san() {
    printf '%s' "$1" | tr '\n\r\t~|' '     ' | cut -c1-"${2:-200}"
}

summary() {
    local final=$1
    local line="ready=$S_READY~ver=$(san "$S_VERSION" 40)~os=$(san "$S_OS" 120)~mode=$MODE"
    line="$line~exit=$S_EXIT~score=$S_SCORE~dur=$(( $(date +%s) - START_TS ))"
    line="$line~ds=$(san "$S_DS" 120)~prof=$(san "$S_PROFILE" 200)~title=$(san "$S_TITLE" 200)"
    line="$line~problem=$(san "$S_PROBLEM" 300)~st_report=$S_ST_REPORT~st_results=$S_ST_RESULTS~st_arf=$S_ST_ARF"
    line="$line~final=$final${S_KV:+~$S_KV}"
    emit summary "$line"
}

problem() {
    S_PROBLEM=$1
    emit problem "$1"
}

finish() {
    emit duration "$(( $(date +%s) - START_TS ))"
    summary 1
    emit done 1
    exit 0
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/oscap.XXXXXX" 2>/dev/null)
if [ -z "$WORK" ] || [ ! -d "$WORK" ]; then
    problem "Could not create a temporary directory"
    finish
fi
trap 'rm -rf "$WORK"' EXIT

# ---- OS identity ---------------------------------------------------------
OS_ID=""; OS_VERSION=""; OS_PRETTY=""
if [ -r /etc/os-release ]; then
    OS_ID=$( . /etc/os-release; printf '%s' "${ID-}")
    OS_VERSION=$( . /etc/os-release; printf '%s' "${VERSION_ID-}")
    OS_PRETTY=$( . /etc/os-release; printf '%s' "${PRETTY_NAME-}")
fi
OS_MAJOR=${OS_VERSION%%.*}
S_OS=$OS_PRETTY
emit os_name "$OS_PRETTY"

# Datastream name stems (ssg-<stem>-ds.xml) for this OS, best first.
stems_for_os() {
    case "$OS_ID" in
        rhel)       echo "rhel$OS_MAJOR" ;;
        centos)     echo "cs$OS_MAJOR centos$OS_MAJOR rhel$OS_MAJOR" ;;
        almalinux)  echo "almalinux$OS_MAJOR rhel$OS_MAJOR" ;;
        rocky)      echo "rl$OS_MAJOR rocky$OS_MAJOR rhel$OS_MAJOR" ;;
        ol)         echo "ol$OS_MAJOR rhel$OS_MAJOR" ;;
        amzn)       if [ "$OS_VERSION" = "2023" ]; then echo "al2023"; else echo "amzn$OS_MAJOR"; fi ;;
        fedora)     echo "fedora" ;;
        ubuntu)     echo "ubuntu${OS_VERSION//./}" ;;
        debian)     echo "debian$OS_MAJOR" ;;
        sles|sled|sle*) echo "sle$OS_MAJOR" ;;
        opensuse*)  echo "opensuse" ;;
        *)          echo "$OS_ID$OS_MAJOR" ;;
    esac
}

# ---- oscap present? ------------------------------------------------------
if ! command -v oscap >/dev/null 2>&1; then
    if [ "$ALLOW_INSTALL" = "1" ]; then
        emit progress "Installing OpenSCAP"
        if command -v dnf >/dev/null 2>&1; then
            dnf install -y openscap-scanner scap-security-guide >"$WORK/install.log" 2>&1
        elif command -v yum >/dev/null 2>&1; then
            yum install -y openscap-scanner scap-security-guide >"$WORK/install.log" 2>&1
        elif command -v zypper >/dev/null 2>&1; then
            zypper --non-interactive install openscap-utils scap-security-guide >"$WORK/install.log" 2>&1
        elif command -v apt-get >/dev/null 2>&1; then
            # Package names differ by release (oscap ships in libopenscap8 on
            # Ubuntu 22.04 and Debian 11, in openscap-scanner on later ones;
            # the guide content is ssg-debderived on Ubuntu, ssg-debian on
            # Debian), so ask apt which of them this release has.
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq >"$WORK/install.log" 2>&1
            APT_PKGS=()
            for p in openscap-scanner libopenscap8; do
                if apt-cache show "$p" >/dev/null 2>&1; then APT_PKGS+=("$p"); break; fi
            done
            case "$OS_ID" in
                debian) APT_SSG="ssg-debian ssg-debderived" ;;
                *)      APT_SSG="ssg-debderived ssg-debian" ;;
            esac
            for p in $APT_SSG; do
                if apt-cache show "$p" >/dev/null 2>&1; then APT_PKGS+=("$p"); break; fi
            done
            if [ "${#APT_PKGS[@]}" -gt 0 ]; then
                apt-get install -y "${APT_PKGS[@]}" >>"$WORK/install.log" 2>&1
            else
                echo "apt has no OpenSCAP packages for this release (is the universe repository enabled?)" >>"$WORK/install.log"
            fi
        else
            problem "OpenSCAP is not installed and no supported package manager was found"
            finish
        fi
        if command -v oscap >/dev/null 2>&1; then
            emit installed yes
        else
            problem "OpenSCAP is not installed and the install failed: $(tail -n 3 "$WORK/install.log" 2>/dev/null | tr '\n' ' ' | cut -c1-200)"
            finish
        fi
    else
        problem "OpenSCAP is not installed"
        finish
    fi
fi
S_VERSION=$(oscap --version 2>/dev/null | sed -n '1s/.*[( ]\([0-9][0-9.]*\)[ )]*$/\1/p')
emit oscap_version "$S_VERSION"

# ---- find and rank datastreams ------------------------------------------
DS_PATHS=()
for dir in /usr/share/xml/scap/ssg/content /usr/share/scap-security-guide /usr/share/xml/scap/ssg \
           /usr/local/share/xml/scap/ssg/content /opt/scap-security-guide; do
    [ -d "$dir" ] || continue
    while IFS= read -r f; do
        [ -n "$f" ] && DS_PATHS+=("$f")
    done < <(find "$dir" -maxdepth 2 -type f -name 'ssg-*-ds.xml' 2>/dev/null | sort)
done

RANKED="$WORK/ranked.txt"
: >"$RANKED"
read -r -a STEMS <<<"$(stems_for_os)"
declare -A SEEN=()
for f in "${DS_PATHS[@]}"; do
    base=${f##*/}
    [ -n "${SEEN[$base]-}" ] && continue
    SEEN[$base]=1
    stem=${base#ssg-}; stem=${stem%-ds.xml}
    rank=100
    for i in "${!STEMS[@]}"; do
        if [ "$stem" = "${STEMS[$i]}" ]; then rank=$i; break; fi
    done
    printf '%03d|%s|%s\n' "$rank" "$base" "$f" >>"$RANKED"
done
sort -t'|' -k1,1 -k2,2 "$RANKED" -o "$RANKED"

if [ ! -s "$RANKED" ]; then
    case "$OS_ID" in
        ubuntu)
            # Ubuntu 22.04's repositories carry oscap but no guide content;
            # Canonical delivers its profiles through Ubuntu Pro.
            problem "OpenSCAP is installed, but this server has no SCAP Security Guide content (ssg-*-ds.xml) and Ubuntu's repositories do not provide it. Canonical ships it with Ubuntu Pro: run 'pro enable usg' and install usg" ;;
        *)
            problem "OpenSCAP is installed, but no SCAP Security Guide content was found (ssg-*-ds.xml). Install the scap-security-guide package for this operating system" ;;
    esac
    finish
fi

n=0
BEST_BASE=""; BEST_PATH=""; BEST_RANK=100
while IFS='|' read -r rank base path; do
    n=$((n + 1))
    [ "$n" -gt 3 ] && break
    # When a datastream matches this operating system, offer only matching
    # ones: content for another release or product (ssg-ol7 on Oracle Linux
    # 8, ssg-firefox) would only report rules as not applicable.
    [ "$n" -eq 1 ] && BEST_RANK=$((10#$rank))
    if [ "$BEST_RANK" -lt 100 ] && [ "$((10#$rank))" -ge 100 ]; then break; fi
    rec=0
    if [ "$n" -eq 1 ] && [ "$((10#$rank))" -lt 100 ]; then rec=1; fi
    [ "$n" -eq 1 ] && { BEST_BASE=$base; BEST_PATH=$path; }
    # Check mode lists the datastreams and their profiles; scan mode prints
    # neither, only the datastream and profile it uses.
    if [ "$MODE" != "scan" ]; then
        emit datastream "$base|$rec"
        oscap info --profiles "$path" 2>/dev/null | head -n 40 | while IFS= read -r line; do
            pid=${line%%:*}
            title=${line#*:}
            [ -n "$pid" ] && emit profile "$base|$pid|$(san "$title" 200)"
        done
    fi
done <"$RANKED"
S_READY=1
emit ready yes

if [ "$MODE" != "scan" ]; then
    finish
fi

# ---- choose datastream and profile --------------------------------------
DS_BASE=$DATASTREAM; DS_PATH=""
if [ -z "$DS_BASE" ]; then
    DS_BASE=$BEST_BASE; DS_PATH=$BEST_PATH
else
    while IFS='|' read -r rank base path; do
        if [ "$base" = "$DS_BASE" ]; then DS_PATH=$path; break; fi
    done <"$RANKED"
fi
if [ -z "$DS_PATH" ]; then
    problem "Datastream $DS_BASE was not found on the server"
    finish
fi

oscap info --profiles "$DS_PATH" >"$WORK/profiles.txt" 2>/dev/null
PROFILE_IDS=()
while IFS= read -r line; do
    pid=${line%%:*}
    [ -n "$pid" ] && PROFILE_IDS+=("$pid")
done <"$WORK/profiles.txt"

CHOSEN=""
if [ -n "$PROFILE" ]; then
    for pid in "${PROFILE_IDS[@]}"; do
        if [ "$pid" = "$PROFILE" ] || [ "$pid" = "xccdf_org.ssgproject.content_profile_$PROFILE" ]; then
            CHOSEN=$pid; break
        fi
    done
else
    IFS=',' read -r -a WANTED <<<"$DEFAULT_PROFILES"
    for want in "${WANTED[@]}"; do
        want=${want// /}
        [ -n "$want" ] || continue
        for pid in "${PROFILE_IDS[@]}"; do
            case "$pid" in
                *"_profile_$want") CHOSEN=$pid; break ;;
            esac
        done
        [ -n "$CHOSEN" ] && break
    done
fi
S_DS=$DS_BASE
if [ -z "$CHOSEN" ]; then
    if [ -n "$PROFILE" ]; then
        problem "Profile $PROFILE is not in $DS_BASE"
    else
        problem "None of the default profiles ($DEFAULT_PROFILES) is in $DS_BASE; choose a profile"
    fi
    finish
fi
S_PROFILE=$CHOSEN
while IFS= read -r line; do
    [ "${line%%:*}" = "$CHOSEN" ] && S_TITLE=${line#*:}
done <"$WORK/profiles.txt"
emit scan_datastream "$DS_BASE"
emit scan_profile "$CHOSEN"
emit scan_profile_title "$S_TITLE"

# ---- run the scan --------------------------------------------------------
EXTRA=()
HELP=$(oscap xccdf eval --help 2>&1)
case "$HELP" in *--thin-results*) EXTRA+=(--thin-results) ;; esac
case "$HELP" in *--skip-validation*) EXTRA+=(--skip-validation) ;; esac

cd "$WORK" || { problem "Could not enter the temporary directory"; finish; }
oscap xccdf eval --profile "$CHOSEN" --results results.xml --results-arf arf.xml \
    --report report.html ${EXTRA[@]+"${EXTRA[@]}"} "$DS_PATH" >"$WORK/oscap.out" 2>"$WORK/oscap.err"
RC=$?
if [ "$RC" -ne 0 ] && [ "$RC" -ne 2 ]; then
    problem "oscap exited with $RC: $(tail -n 5 "$WORK/oscap.err" | tr '\n' ' ' | cut -c1-200)"
    finish
fi
S_EXIT=$RC
emit exit_code "$RC"

# ---- summarise results.xml (grep/awk/sed only) --------------------------
: >"$WORK/kv.txt"; : >"$WORK/failed.txt"
if [ -s results.xml ]; then
    # One tag per line, so attribute order, prefixes and line wrapping do not matter.
    tr '\n\r' '  ' <results.xml | sed 's/</\n</g' >"$WORK/tags.txt"
    awk -v kvfile="$WORK/kv.txt" -v failfile="$WORK/failed.txt" '
    function attr(s, name,    v) {
        if (match(s, "[ \t]" name "=\"[^\"]*\"")) {
            v = substr(s, RSTART, RLENGTH)
            sub("^[ \t]" name "=\"", "", v); sub("\"$", "", v)
            return v
        }
        return ""
    }
    function trim(s) { gsub(/^[ \t]+|[ \t]+$/, "", s); return s }
    BEGIN { in_rr = 0; total = 0; rules = 0 }
    /^<([A-Za-z0-9_.-]+:)?rule-result[ \t>\/]/ {
        cur_id = attr($0, "idref"); cur_sev = attr($0, "severity")
        if (cur_sev != "high" && cur_sev != "medium" && cur_sev != "low") cur_sev = "unknown"
        in_rr = 1; next
    }
    /^<\/([A-Za-z0-9_.-]+:)?rule-result>/ { in_rr = 0; next }
    in_rr && /^<([A-Za-z0-9_.-]+:)?result>/ {
        v = $0; sub(/^<[^>]*>/, "", v); v = trim(v)
        count[v]++; rules++
        if (v == "fail") {
            sev[cur_sev]++
            sub(/^xccdf_org\.ssgproject\.content_rule_/, "", cur_id)
            total++
            if (total <= 2000) { fid[total] = cur_id; fsev[total] = cur_sev }
        }
        next
    }
    /^<([A-Za-z0-9_.-]+:)?score[ \t>]/ {
        if (score == "" && index($0, "urn:xccdf:scoring:default")) {
            v = $0; sub(/^<[^>]*>/, "", v); score = trim(v)
        }
        next
    }
    END {
        n = split("pass fail error unknown notapplicable notchecked notselected informational fixed", names, " ")
        for (i = 1; i <= n; i++) printf "%s=%d\n", names[i], count[names[i]] + 0 > kvfile
        printf "sev_high=%d\nsev_medium=%d\nsev_low=%d\nsev_unknown=%d\nrules=%d\n", sev["high"] + 0, sev["medium"] + 0, sev["low"] + 0, sev["unknown"] + 0, rules > kvfile
        if (score != "") printf "score=%s\n", score > kvfile
        m = split("high medium low unknown", order, " ")
        shown = 0
        for (o = 1; o <= m; o++)
            for (i = 1; i <= total && i <= 2000; i++)
                if (fsev[i] == order[o] && shown < 1000) { printf "%s %s\n", order[o], fid[i] > failfile; shown++ }
    }' "$WORK/tags.txt"
    S_SCORE=$(sed -n 's/^score=//p' "$WORK/kv.txt" | head -n 1)
    S_KV=$(grep -v '^score=' "$WORK/kv.txt" | tr '\n' '~' | sed 's/~$//')
else
    problem "oscap produced no results file"
fi
summary 0
while IFS= read -r line; do
    emit failed "$(san "$line" 220)"
done <"$WORK/failed.txt"

# ---- gzip and upload -----------------------------------------------------
gzip -c results.xml >results.xml.gz 2>/dev/null
gzip -c arf.xml >arf.xml.gz 2>/dev/null

# Quote a value for a curl config file line.
cfg_quote() {
    local v=$1
    v=${v//\\/\\\\}
    v=${v//\"/\\\"}
    printf '"%s"' "$v"
}

upload() {
    local file=$1 url=$2 name=$3 reason ok=0
    if [ -z "$url" ]; then return; fi
    if [ ! -s "$file" ]; then
        emit stored "$name|no|the file was not produced"
    elif ! command -v curl >/dev/null 2>&1; then
        emit stored "$name|no|curl is not installed on the server"
    else
        local flags=(-sS -f --retry 2 --max-time 300)
        [ "$UP_INSECURE" = "1" ] && flags+=(-k)
        # The URL and the token go to curl on stdin (-K -), never on its
        # command line, so they do not show in the process list.
        if {
            printf 'url = %s\n' "$(cfg_quote "$url")"
            printf 'upload-file = %s\n' "$(cfg_quote "$file")"
            [ -n "$UP_TOKEN" ] && printf 'header = %s\n' "$(cfg_quote "X-Oscap-Token: $UP_TOKEN")"
        } | curl "${flags[@]}" -K - >/dev/null 2>"$WORK/curl.err"; then
            ok=1
            emit stored "$name|yes|"
        else
            # curl -sS prints "curl: (NN) reason" and then, for some errors,
            # several lines of advice; keep the reason line.
            reason=$(grep -m 1 '^curl: (' "$WORK/curl.err" | cut -c1-200)
            case "$reason" in
                "curl: (60)"*)
                    reason="the server does not trust the certificate of the upload address (curl 60); install the issuing CA on the server, or for appliance storage turn on Skip certificate check in the Compliance settings" ;;
                "") reason=$(head -n 1 "$WORK/curl.err" | cut -c1-200) ;;
            esac
            emit stored "$name|no|${reason:-upload failed}"
        fi
    fi
    case "$name" in
        report.html) S_ST_REPORT=$ok ;;
        results.xml.gz) S_ST_RESULTS=$ok ;;
        arf.xml.gz) S_ST_ARF=$ok ;;
    esac
}
upload report.html "$UP_REPORT" report.html
upload results.xml.gz "$UP_RESULTS" results.xml.gz
upload arf.xml.gz "$UP_ARF" arf.xml.gz

# ---- hold for collection -------------------------------------------------
# A server that cannot reach an upload address gets HOLD_DIR and no upload
# URLs: the files wait in a root-only directory, and CloudBolt reads them over
# the channel it ran this script through and then removes the directory. One a
# job that died left behind is removed by the next scan.
if [ -n "$HOLD_DIR" ]; then
    find "${HOLD_DIR%/*}" -maxdepth 1 -type d -name 'oscap-hold-*' -mmin +180 -exec rm -rf {} + 2>/dev/null
    rm -rf "$HOLD_DIR"
    if mkdir -m 700 "$HOLD_DIR" 2>/dev/null; then
        for name in report.html results.xml.gz arf.xml.gz; do
            if [ -s "$name" ]; then mv "$name" "$HOLD_DIR/$name"; fi
        done
        emit held 1
    else
        emit held 0
    fi
fi

finish
