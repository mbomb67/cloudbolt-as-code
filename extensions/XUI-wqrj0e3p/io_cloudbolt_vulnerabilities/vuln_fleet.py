"""
The fleet-wide work of the Vulnerabilities extension: which servers a job may scan, the
cleanup of servers that are gone, and the three jobs that run without a person.

    run_rematch     nightly. Removes what is stored for deleted servers, updates the
                    vulnerability database and the Microsoft security data, and matches the
                    stored software lists (Linux) and Windows builds of the servers that were
                    last matched against older data of their own kind.
    run_inventory   weekly. Starts a scan (vuln_jobs.start) of every server that can be
                    scanned without a person.
    run_provision   after a build, when the setting is on. Starts a scan of each new server
                    as its own job; the build never waits for it and never fails because of it.
    run_resource_scan   when someone chooses Scan all servers on a resource. Starts a scan of
                    each of the resource's servers that the person may scan, as that person.

Rules that hold everywhere here:
  * A server in an Unassigned group is never scanned and never re-matched, and what is stored
    for it is removed (forget_stale).
  * A server that cannot be scanned unattended is skipped and counted, never failed.
  * A resource scan runs as the person who started it: each server is checked against that person's
    permissions immediately before its scan starts, so a change made after the request is honoured.
  * A job's worker threads never use the Django ORM (a thread has its own database connection
    that nothing closes): every ORM call runs in the job's own thread. The worker threads of the
    re-match only run the scanner and parse its report; the main thread saves the results.
"""
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from accounts.models import Group, UserProfile
from infrastructure.models import Server
from jobs.models import Job
from utilities.logger import ThreadLogger

from . import vuln_db, vuln_hooks, vuln_jobs, vuln_msrc, vuln_repo, vuln_store as store, vuln_tools as tools

logger = ThreadLogger(__name__)

# A weekly scan skips a server whose stored software list is younger than this.
RECENT_S = 20 * 3600
# Seconds between looks at the scans the weekly inventory started.
POLL_S = 5
# Seconds a re-match waits for a finished match before it looks at the cancel request again.
WAIT_S = 5
# CloudBolt's job engine cancels any job that runs longer than the global job timeout (8 hours by default;
# jobengine/jobengine.py lines 895-926), and a cancelled job skips its cleanup. Each job here stops taking
# on new work after this long, finishes what is in flight and ends normally, so the next run carries on.
JOB_BUDGET_S = 6 * 3600
CANCEL_STATUSES = ("TO_CANCEL", "CANCELED")
# Server statuses (infrastructure Server.status). The Servers list shows ACTIVE and MODIFY (a resize is
# transient and must not make a server flicker out); only HISTORICAL servers are purged. DECOM (being deleted),
# PROV and PROVFAILED are neither shown nor purged, as they may still become ACTIVE (a failed delete returns the
# server to ACTIVE): they are hidden until they settle.
CURRENT_STATUSES = ("ACTIVE", "MODIFY")
PURGE_STATUSES = ("HISTORICAL",)
# reconcile_servers reads CloudBolt at most this often per web process, and purges at most this many per call.
RECONCILE_MIN_S = 15
MAX_FORGET_PER_CALL = 50
SWEEP_CHUNK = 500
BAD_STATUSES = ("FAILURE", "CANCELED", "TO_CANCEL")

# Reason codes of eligibility() and the skips the jobs count, in words.
REASONS = {
    "historical": "historical",
    "unassigned": "in an Unassigned group",
    "unsupported_os": "not Linux or Windows",
    "hidden": "not shown the tab by the settings",
    "powered_off": "powered off",
    "no_script_channel": "no way to run a script",
    "no_address": "no address",
    "no_credentials": "no credentials",
    "unknown": "could not be checked",
    "recent": "scanned in the last 20 hours",
    "refused": "refused to start",
    "not_active": "not active",
    "gone": "no longer in CloudBolt",
    "no_list": "no stored software list",
    "scanning": "scan in progress",
    "changed": "scanned again while it ran",
    "left_resource": "no longer in the resource",
    "no_permission": "without permission to scan",
    "not_set_up": "scanner not set up",
    "no_site_url": "no portal site URL",
    "no_windows_data": "no Microsoft security data",
    "unsupported_windows": "a Windows version this extension does not cover",
}
# A db_built that is a Microsoft index stamp ("2026-11-10.26edfc27ff"), not a Grype build time: a date, a dot, a hash.
MSRC_STAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.")
# What stops a server of each kind from being scanned when the data it needs is missing (prerequisites()).
KIND_PROBLEM = {"linux": "not_set_up", "windows": "no_windows_data"}


# ---------------------------------------------------------------- small helpers
def default_base_url():
    """The default portal's site URL without a request, or "" when it is not set. Servers are told to
    send their software list here, so it is never built from a request's Host header."""
    try:
        from portals.models import PortalConfig

        # portals/models.py get_current_portal (line 910): with no request it returns the default portal
        # (the only one, or is_default, or the first); site_url (line 891) is https://<domain>/ or None.
        portal = PortalConfig.get_current_portal(None)
        return ((portal.site_url if portal else None) or "").rstrip("/")
    except Exception:
        logger.exception("Vulnerabilities: could not read the default portal's site URL")
        return ""


def _progress(job, message, done=None, total=None):
    """job.set_progress (jobs/models.py line 1192), from one thread at a time when the run has threads
    (vuln_jobs.progress_guard). Never raises."""
    if job is None:
        return
    try:
        with vuln_jobs.progress_guard():
            if done is None:
                job.set_progress(message)
            else:
                job.set_progress(message, tasks_done=int(done), total_tasks=int(total))
    except Exception:
        logger.exception("Vulnerabilities: could not set the job's progress")


def _clock():
    """Seconds on a clock that only moves forward; the jobs measure their own budget with it."""
    return time.monotonic()


def _cancelled(job):
    """True when the job was asked to stop. A cancel raises CancelJobException (utilities/exceptions.py
    line 26) into the job's thread; this lets a loop stop cleanly between two items as well."""
    try:
        found = Job.objects.filter(id=job.id).values_list("status", flat=True).first()
    except Exception:
        logger.exception("Vulnerabilities: could not read the job's status")
        return False
    return found in CANCEL_STATUSES


def _scan_running(server_id):
    """True when a scan is running on the server, or when its state cannot be read (then it counts as busy)."""
    try:
        return vuln_jobs.active_job(server_id) is not None
    except Exception:
        # Any failure, not only store.StoreError: the module may have been reloaded while this job ran, and
        # an error of the older class would not match the new one.
        return True


def _started_id(result):
    """The id of the Job vuln_jobs.start returned, or None when it returned a Refusal. Decided by the
    integer id a Job has and a Refusal does not, not by isinstance: every job file reloads the package's
    modules, so during a long job an older Refusal need not be an instance of the current class."""
    value = getattr(result, "id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _store_unreadable(exc):
    return "The findings store could not be opened, so nothing was done: {}".format(vuln_jobs.clean(str(exc), 200))


def _words(counts, order=None):
    """"3 powered off, 1 no address" from {code: count}; empty when there are none."""
    parts = []
    for code in (order or sorted(counts)):
        if counts.get(code):
            parts.append("{} {}".format(counts[code], REASONS.get(code, code)))
    return ", ".join(parts)


def _plural(count, noun):
    return "{} {}{}".format(count, noun, "" if count == 1 else "s")


# ---------------------------------------------------------------- what each kind of server needs
def prerequisites(base_url):
    """(linux problem, windows problem): "" when servers of that kind can be scanned now, else a reason code of
    REASONS. A Linux server needs the scanner and the portal's site URL; a Windows server needs only the Microsoft
    security data on the appliance."""
    try:
        ready = bool(tools.status()["ready"])
    except Exception:
        logger.exception("Vulnerabilities: could not read the state of the scanner")
        ready = False
    linux = "not_set_up" if not ready else ("" if base_url else "no_site_url")
    windows = "" if vuln_jobs.msrc_ready() else "no_windows_data"
    return linux, windows


def prerequisite_for(server, problems):
    """The reason code that keeps this server from being scanned now, or "". problems is prerequisites()."""
    return problems[1] if vuln_jobs.is_windows(server) else problems[0]


def nothing_can_run(problems):
    """True when neither a Linux nor a Windows server can be scanned."""
    return bool(problems[0] and problems[1])


def not_ready_message(problems):
    """The sentence for a job that cannot start any scan: the Linux one, as it always was."""
    return vuln_jobs.NOT_SET_UP if problems[0] == "not_set_up" else vuln_jobs.NO_SITE_URL


# ---------------------------------------------------------------- which servers
def eligibility(server, visibility):
    """"" when an unattended job may scan the server, else a short reason code: historical, not_active (any
    other status than ACTIVE, such as a failed build), unassigned, unsupported_os (neither Linux nor Windows),
    hidden, powered_off, unsupported_windows (a Windows version whose last scan failed as not covered),
    no_script_channel, no_address, no_credentials (or unknown when a check itself failed). No network call (one
    small state file is read for a Windows server); the resource handler may be None."""
    try:
        if server.status == "HISTORICAL":
            return "historical"
        if server.status != "ACTIVE":
            return "not_active"
        if vuln_jobs.in_unassigned_group(server):
            return "unassigned"
        if not vuln_jobs.is_supported(server):
            return "unsupported_os"
        if not vuln_jobs.shows_tab(server, visibility):
            return "hidden"
        if server.power_status != "POWERON":
            return "powered_off"
        if vuln_jobs.is_windows(server) and _unsupported_windows(server):
            return "unsupported_windows"
        if vuln_jobs._is_openshift_vm(server):
            # The scan reaches these through the API tunnel (vuln_jobs._execute), which needs no address
            # and logs in on its own.
            return ""
        handler = server.resource_handler
        # infrastructure/models.py Server.execute_script (line 4024) refuses a handler that cannot run scripts.
        if handler is None or not handler.cast().can_run_scripts_on_servers:
            return "no_script_channel"
        # Server.use_tech_specific_script_execution (line 4147): the handler's own channel (VMware tools)
        # needs neither an address nor credentials from CloudBolt.
        if server.use_tech_specific_script_execution():
            return ""
        # Server.get_preferred_ip (line 3840): the address execute_script connects to.
        if not server.get_preferred_ip():
            return "no_address"
        try:
            # Server.get_credentials (line 3857); arm_for_use=False writes nothing. It raises when a key file
            # the server names is not on CloudBolt.
            credentials = server.get_credentials(arm_for_use=False)
        except Exception:
            return "no_credentials"
        if not isinstance(credentials, dict) or not (credentials.get("password") or credentials.get("keyfile")):
            return "no_credentials"
        return ""
    except Exception:
        logger.exception("Vulnerabilities: could not check whether server %s can be scanned", getattr(server, "id", ""))
        return "unknown"


def unassigned_ids():
    """The ids of the Unassigned groups (the general one and every tenant's default group; accounts/models.py
    Group.unassigned, common/configuration.py UnassignedManager.ids), as a set: one query for a whole request."""
    return set(Group.unassigned.ids())


def quick_eligibility(server, problems, unassigned, visibility):
    """"" when updates may be offered for the server, else a reason code: the checks of vuln_patch.eligibility
    (unsupported_os, historical, not_active, unassigned, hidden, powered_off, then the prerequisite of its kind)
    that need no query per server, for the rows of a page and "Select all". It never says no where
    vuln_patch.eligibility would say yes: every check here is the same test, or a subset of one. What it leaves
    out (a Windows version the last scan found not covered, the script channel, the address, the credentials) is
    found when the update is submitted (vuln_patch.eligibility runs there for every selected server) or by the
    job. problems is prerequisites(), unassigned is unassigned_ids() and visibility the "visibility" setting, each
    read once per request. The server needs its group id, status, power state, tags (a prefetch) and OS family
    with its parents (select_related) loaded; nothing else is touched. Never raises."""
    try:
        if not vuln_jobs.is_supported(server):
            return "unsupported_os"
        if server.status == "HISTORICAL":
            return "historical"
        if server.status != "ACTIVE":
            return "not_active"
        if server.group_id in unassigned:
            return "unassigned"
        if not vuln_jobs.shows_tab(server, visibility):
            return "hidden"
        if server.power_status != "POWERON":
            return "powered_off"
        return prerequisite_for(server, problems)
    except Exception:
        logger.exception("Vulnerabilities: could not check whether server %s may be updated", getattr(server, "id", ""))
        return "unknown"


def _unsupported_windows(server):
    """True when the server's last scan failed because its Windows version is not one the Microsoft data covers
    (vuln_jobs.UNSUPPORTED_WINDOWS is stored in that scan's entry). Every unattended scan of it would fail the
    same way, so the jobs skip it; a later successful scan (the tab's button still works) replaces the entry and
    clears this. A state that cannot be read does not skip the server."""
    try:
        last = store.read_state(server.id).get("last")
    except Exception:
        return False
    return bool(isinstance(last, dict) and last.get("status") == "failed"
                and last.get("reason") == vuln_jobs.UNSUPPORTED_WINDOWS)


def candidates(visibility):
    """The ACTIVE, powered-on servers that are not in an Unassigned group, in one query (with their
    group, OS family and resource handler, and their tags read ahead), to feed eligibility()."""
    # accounts/models.py line 1792 (Group.unassigned) and common/configuration.py line 130 (UnassignedManager.ids):
    # the general Unassigned group and every tenant's default group.
    unassigned = list(Group.unassigned.ids())
    found = Server.objects.filter(status="ACTIVE", power_status="POWERON")
    if unassigned:
        found = found.exclude(group__in=unassigned)
    if visibility == "label":
        found = found.filter(tags__name=vuln_jobs.LABEL).distinct()
    return found.select_related("group", "os_family", "resource_handler").prefetch_related("tags").order_by("id")


def _stale_ids(ids):
    """The ids among ids whose server is gone, HISTORICAL or in an Unassigned group; None when that
    cannot be decided. Only a server row that was read successfully keeps an id out of the answer
    when it is live, and any failure reading them gives None, so nothing is forgotten."""
    try:
        unassigned = set(Group.unassigned.ids())
        live = {}
        for server_id, status, group_id in Server.objects.filter(id__in=sorted(ids)).values_list(
            "id", "status", "group_id"
        ):
            live[server_id] = (status, group_id)
    except Exception:
        logger.exception("Vulnerabilities: could not read the servers; nothing is forgotten")
        return None
    stale = []
    for server_id in ids:
        found = live.get(server_id)
        if found is None or found[0] in PURGE_STATUSES or found[1] in unassigned:
            stale.append(server_id)
    return sorted(stale)


def _forget(ids):
    """Remove everything stored for each of these servers: findings (vuln_db.forget_server), stored software
    list and scan history. A server with a scan in progress is left alone, and a failure on one is logged and
    does not stop the rest. The status and group of the ids are read again, in one query, right before anything is
    deleted: an id whose server is current again (a sweep can be seconds old) is skipped, and when that read
    fails nothing is deleted. The files go first and the findings row last, so a failure leaves the row and the
    id is tried again by the next sweep. Returns the ids that were forgotten."""
    ids = list(ids)
    if not ids:
        return []
    purgeable = _stale_ids(ids)
    if purgeable is None:
        return []
    purgeable = set(purgeable)
    done = []
    for server_id in ids:
        try:
            if server_id not in purgeable or _scan_running(server_id):
                continue
            vuln_repo.forget(server_id)
            store.forget_state(server_id)
            vuln_db.forget_server(server_id)
            done.append(server_id)
        except Exception:
            logger.exception("Vulnerabilities: could not forget server %s", server_id)
    return done


def forget_stale(progress=None):
    """Remove everything stored for servers that are gone, historical or in an Unassigned group:
    their findings (vuln_db.forget_server), stored software list and scan history. Never touches a
    server with a scan in progress, nor one that exists, is not historical and is in a normal group.
    Returns {"forgotten": n, "kept": n}."""
    result = {"forgotten": 0, "kept": 0}
    try:
        ids = {int(row["server_id"]) for row in vuln_db.known_servers()}
        ids |= set(store.known_state_ids())
        ids |= set(vuln_repo.known_ids())
    except Exception:
        logger.exception("Vulnerabilities: could not list the servers that have stored data")
        return result
    if not ids:
        return result
    stale = _stale_ids(ids)
    if not stale:
        result["kept"] = len(ids)
        return result
    if progress is not None:
        progress("Removing what is stored for {}".format(_plural(len(stale), "server that is gone")))
    result["forgotten"] = len(_forget(stale))
    result["kept"] = len(ids) - result["forgotten"]
    return result


def owner_label(first, last, username):
    """The owner as the Servers list shows them: the full name when there is one, else the username, else "".
    The one helper is vuln_store.owner_label, which the scan snapshot uses as well."""
    return store.owner_label(first, last, username)


_reconcile_lock = threading.Lock()
_reconcile_state = {"stamp": None, "result": None}


def _reconcile_result(hidden=(), skipped=False, **counts):
    result = {"checked": 0, "forgotten": 0, "refreshed": 0, "hidden": sorted(set(hidden)), "skipped": skipped}
    result.update(counts)
    return result


def reconcile_servers(max_forget=MAX_FORGET_PER_CALL, force=False):
    """Make what the findings store lists agree with CloudBolt, for a web request to call before it reads.

    Servers that are gone, HISTORICAL or in an Unassigned group are purged (at most max_forget per
    call, each in its own short transaction; the store's write lock is never held across the ORM sweep), and
    the stored hostname, group, environment, owner and OS build of the servers that are current are refreshed.
    Returns {"checked", "forgotten", "refreshed", "hidden", "skipped"}: hidden are the ids that must not be
    listed though they were not purged (not current but not purgeable, such as DECOM or PROV, a scan running, a purge
    that failed, or beyond max_forget). The CloudBolt sweep runs at most once per RECONCILE_MIN_S unless force:
    a call inside that window answers the previous result with skipped True. Never raises; when CloudBolt
    cannot be read nothing is forgotten and skipped is True."""
    try:
        with _reconcile_lock:
            previous = _reconcile_state["result"]
            stamp = _reconcile_state["stamp"]
            now = _clock()
            if not force and stamp is not None and 0 <= now - stamp < RECONCILE_MIN_S:
                return _reconcile_result(hidden=(previous or {}).get("hidden", ()), skipped=True)
            _reconcile_state["stamp"] = now  # claimed: a failure below is not retried at once either
        result = _reconcile(max_forget, (previous or {}).get("hidden", ()))
        with _reconcile_lock:
            _reconcile_state["result"] = result
        return dict(result, hidden=list(result["hidden"]))
    except Exception:
        logger.exception("Vulnerabilities: could not reconcile the stored servers with CloudBolt")
        return _reconcile_result(skipped=True)


def _reconcile(max_forget, kept_hidden):
    """The work of reconcile_servers. kept_hidden is what the previous sweep hid, still hidden when this
    one cannot read CloudBolt."""
    try:
        ids = sorted({int(row["server_id"]) for row in vuln_db.known_servers()})
    except Exception:
        logger.exception("Vulnerabilities: could not list the servers that have stored data")
        return _reconcile_result(hidden=kept_hidden, skipped=True)
    if not ids:
        # known_servers cannot tell an empty store from a busy or unreadable one, so the last hidden list is kept
        # in both cases (ids of servers that no longer exist hide nothing).
        return _reconcile_result(hidden=kept_hidden, skipped=bool(kept_hidden))
    try:
        unassigned = set(Group.unassigned.ids())
        live = {}
        for start in range(0, len(ids), SWEEP_CHUNK):
            for found in Server.objects.filter(id__in=ids[start:start + SWEEP_CHUNK]).values_list(
                "id", "status", "group_id", "hostname", "group__name", "environment__name",
                "owner__user__first_name", "owner__user__last_name", "owner__user__username", "os_build__name"
            ):
                live[found[0]] = found[1:]
    except Exception:
        logger.exception("Vulnerabilities: could not read the servers; nothing is forgotten")
        return _reconcile_result(hidden=kept_hidden, skipped=True)
    stale, hidden, current = [], [], {}
    for server_id in ids:
        found = live.get(server_id)
        if found is None or found[0] in PURGE_STATUSES or found[1] in unassigned:
            stale.append(server_id)
        elif found[0] in CURRENT_STATUSES:
            current[server_id] = found
        else:
            hidden.append(server_id)
    limit = max(0, int(max_forget))
    forgotten = _forget(stale[:limit])
    done = set(forgotten)
    hidden += [server_id for server_id in stale if server_id not in done]
    return _reconcile_result(hidden=hidden, checked=len(ids), forgotten=len(forgotten),
                             refreshed=_refresh(current))


def _refresh(current):
    """Write the names of the current servers that differ from what is stored; returns how many changed.
    Nothing is written when none differ, or when the stored names cannot be read."""
    try:
        stored = vuln_db.server_attributes()
        changed = []
        for server_id, found in current.items():
            fresh = {
                "server_id": server_id, "hostname": found[2] or "", "group_name": found[3] or "",
                "environment": found[4] or "", "owner": owner_label(found[5], found[6], found[7]),
                "os_build": found[8] or "",
            }
            have = stored.get(server_id)
            if have is not None and any((have.get(key) or "") != fresh[key] for key in
                                        ("hostname", "group_name", "environment", "owner", "os_build")):
                changed.append(fresh)
        return vuln_db.update_server_attributes(changed) if changed else 0
    except Exception:
        logger.exception("Vulnerabilities: could not refresh the stored server names")
        return 0


# ---------------------------------------------------------------- nightly re-match
def _match_worker(item):
    """Match one stored software list and parse the report. Runs in a worker thread: scanner, files
    and parsing only, no Django ORM. The pool may be larger than the match limit; a thread waits in
    vuln_jobs.match_and_parse for a slot. Returns what vuln_match.parse_grype returns."""
    out_json = store.contained(store.WORK_DIR, "rematch-{}-{}.grype.json".format(item["server_id"], item["scan_id"]))
    store.ensure_dir(store.WORK_DIR)
    return vuln_jobs.match_and_parse(item["path"], out_json)


def _rematch_items(known, builts, counts):
    """The servers to re-match (oldest match first) as work items; counts gets the skips. builts is
    {"linux": the Grype database build, "windows": the Microsoft data stamp}, a kind with no data left out.
    Each server's stored software list says which kind it is (vuln_jobs.inventory_kind) and the row's db_built
    is compared with the stamp of that kind. A row with no stored list is left alone when its db_built equals
    either stamp. Runs in the job's thread: it reads Server rows, the state files, the stored lists and the jobs."""
    current = {built for built in builts.values() if built}
    todo = []
    for row in known:
        scan_id = row.get("scan_id") or ""
        path = vuln_repo.inventory_path(int(row["server_id"]), scan_id) if scan_id else None
        kind, facts = ("linux", None)
        if path is not None:
            kind, facts = vuln_jobs.inventory_kind(path)
        stamp = row.get("db_built") or ""
        if path is None:
            if stamp in current:
                continue
        elif not builts.get(kind):
            # No data of its kind on the appliance (yet): nothing to match against, so it is left as it is.
            counts[KIND_PROBLEM[kind]] += 1
            continue
        elif stamp == builts[kind]:
            continue
        todo.append((row, path, kind, facts))
    todo.sort(key=lambda entry: entry[0].get("matched_at") or "")
    if not todo:
        return []
    servers = {
        server.id: server
        for server in Server.objects.filter(id__in=[int(entry[0]["server_id"]) for entry in todo]).select_related(
            "group", "environment")
    }
    items = []
    for row, path, kind, facts in todo:
        server_id = int(row["server_id"])
        server = servers.get(server_id)
        if server is None:
            counts["gone"] += 1
        elif server.status == "HISTORICAL":
            counts["historical"] += 1
        elif vuln_jobs.in_unassigned_group(server):
            counts["unassigned"] += 1
        else:
            if path is None:
                counts["no_list"] += 1
            elif _scan_running(server_id):
                counts["scanning"] += 1
            else:
                items.append({"server_id": server_id, "scan_id": row.get("scan_id") or "", "path": path,
                              "server": server, "kind": kind, "facts": facts})
    return items


def _requalify(server_id):
    """(Server, "") when the server still exists, is not historical and is not in an Unassigned group, read
    fresh now (one small query, in the job's thread); else (None, reason): gone, historical, unassigned,
    or unknown when the row cannot be read. A re-match can run for hours, so the selection made when it
    started is not trusted when a server is submitted or saved."""
    try:
        server = Server.objects.filter(id=server_id).select_related(
            "group", "environment", "owner__user", "os_build").first()
    except Exception:
        logger.exception("Vulnerabilities: could not read server %s again", server_id)
        return None, "unknown"
    if server is None:
        return None, "gone"
    if server.status == "HISTORICAL":
        return None, "historical"
    if vuln_jobs.in_unassigned_group(server):
        return None, "unassigned"
    return server, ""


def _changed_meanwhile(item):
    """True when a match that failed is explained by a scan that finished or started on the server while it
    ran (vuln_repo.prune then removed the software list that was being matched). Never raises."""
    try:
        if _scan_running(item["server_id"]):
            return True
        row = vuln_db.server_summary(item["server_id"])
        return bool(row) and row.get("scan_id") != item["scan_id"]
    except Exception:
        return False


def _names(server, row):
    """hostname, group, environment, owner and OS build as the Server has them now; what is stored when they
    cannot be read (the owner and the OS build each on their own)."""
    try:
        names = {
            "hostname": server.hostname,
            "group_name": server.group.name if server.group else "",
            "environment": server.environment.name if server.environment else "",
        }
    except Exception:
        names = {key: row.get(key) for key in ("hostname", "group_name", "environment")}
    try:
        user = server.owner.user if server.owner else None
        names["owner"] = owner_label(user.first_name, user.last_name, user.username) if user is not None else ""
    except Exception:
        names["owner"] = row.get("owner") or ""
    try:
        names["os_build"] = server.os_build.name if server.os_build else ""
    except Exception:
        names["os_build"] = row.get("os_build") or ""
    return names


def _save_rematch(item, parsed, built, counts):
    """Store one re-match, in the job's thread. The result is dropped when a scan is running on the server
    or its stored scan is no longer the one that was matched. Returns "changed", "unchanged" or "" (dropped)."""
    server_id = item["server_id"]
    # The start lock keeps a scan from being queued between the checks and the save (vuln_jobs.start takes it).
    with store.start_lock(server_id):
        if _scan_running(server_id):
            counts["scanning"] += 1
            return ""
        row = vuln_db.server_summary(server_id)
        if not row or row.get("scan_id") != item["scan_id"]:
            counts["changed"] += 1
            return ""
        # Read again, not the selection of hours ago: the server may be gone, historical or now Unassigned.
        server, reason = _requalify(server_id)
        if reason:
            counts[reason] += 1
            return ""
        updated = dict(row)
        updated.update(_names(server, row))
        # The snapshot columns stay as they were; only the match time and the database build move. The build
        # is the scanner's own record (tools.status), not the report's, so the comparison next night is exact.
        updated["matched_at"] = store.stamp()
        updated["db_built"] = built
        parsed["db_built"] = built
        saved = vuln_db.replace_server(updated, parsed)
    # replace_server says whether it took the fast path because the matches were equal.
    return "unchanged" if (saved or {}).get("unchanged") else "changed"


def run_rematch(job, **kwargs):
    """Plugin entry point (via vuln_rematch_job.py). Returns (status, output, errors, outputs)."""
    try:
        return _rematch(job)
    except Exception as exc:
        logger.exception("Vulnerabilities: the nightly re-match failed")
        return "FAILURE", "", "The nightly re-match failed: {}".format(vuln_jobs.clean(str(exc), 300)), {}


def _match_windows(item, index, built, counts):
    """Re-match one Windows server in the job's own thread (a lookup in the Microsoft data: no match slot, no
    scanner, no worker). Returns "changed", "unchanged" or "" (dropped); raises when the lookup fails."""
    server, reason = _requalify(item["server_id"])
    if reason:
        counts[reason] += 1
        return ""
    result = vuln_msrc.match(item["facts"], index)
    return _save_rematch(dict(item, server=server), {"matches": result["matches"]},
                         result.get("db_built") or built, counts)


def _microsoft_in_use(known):
    """True when the Microsoft security data has been set up on this appliance, or a known server was last matched
    against it (its db_built is a Microsoft stamp). Never raises."""
    try:
        if vuln_msrc.status().get("present"):
            return True
    except Exception:
        logger.exception("Vulnerabilities: could not read the state of the Microsoft security data")
    return any(MSRC_STAMP_RE.match(str(row.get("db_built") or "")) for row in known or [])


def _rematch(job):
    try:
        vuln_db.upgrade()   # also the one-time upgrade of a store the previous release left
    except Exception as exc:
        logger.exception("Vulnerabilities: the findings store could not be opened")
        return "FAILURE", "", _store_unreadable(exc), {}
    linux_ready = bool(tools.status()["ready"])
    if not linux_ready and not vuln_jobs.msrc_ready():
        return "FAILURE", "", vuln_jobs.NOT_SET_UP, {}
    notes = []

    def progress(message):
        _progress(job, message)

    forgotten = forget_stale(progress)["forgotten"]

    if linux_ready:
        progress("Updating the vulnerability database")
        try:
            tools.update_database(progress)
        except Exception as exc:
            logger.exception("Vulnerabilities: the database update failed; matching with the database it has")
            notes.append("The vulnerability database could not be updated ({}); the one already there was used.".format(
                vuln_jobs.clean(exc.public if isinstance(getattr(exc, "public", None), str) else str(exc), 150)))
    known = vuln_db.known_servers()
    # The download is for an appliance that uses the Microsoft data: it has been set up here, or a Windows server
    # has been scanned. A Linux-only or air-gapped appliance does not try it every night and add a failure note.
    if _microsoft_in_use(known):
        progress("Updating the Microsoft security data")
        try:
            tools.update_microsoft(progress)
        except Exception as exc:
            logger.exception("Vulnerabilities: the Microsoft data update failed; matching with the data it has")
            notes.append("The Microsoft security data could not be updated ({}); the data already there was used.".format(
                vuln_jobs.clean(exc.public if isinstance(getattr(exc, "public", None), str) else str(exc), 150)))
    builts = {}
    if linux_ready:
        built = tools.status()["db"]["built"]
        if built:
            builts["linux"] = built
    windows_index = None
    if vuln_jobs.msrc_ready():
        windows_index = vuln_msrc.load_index()
        windows_built = vuln_msrc.status()["built"]
        if windows_index is not None and windows_built:
            builts["windows"] = windows_built
    if not builts:
        return "WARNING", "The build date of the vulnerability database is not known, so no server was re-matched.", "", {}
    built = builts.get("linux", "")

    counts = {key: 0 for key in REASONS}
    items = _rematch_items(known, builts, counts)
    windows_items = [item for item in items if item["kind"] == "windows"]
    items = [item for item in items if item["kind"] != "windows"]
    total = len(items) + len(windows_items)
    rematched = unchanged = failed = done = 0
    stopped = out_of_time = False
    left = 0
    started_at = _clock()
    workers = max(1, min(4, int(store.get_settings()["rematch_workers"])))

    if windows_items:
        progress("Matching {} against the Microsoft security data".format(_plural(len(windows_items), "Windows server")))
        for item in windows_items:
            if _cancelled(job):
                stopped = True
                break
            done += 1
            try:
                outcome = _match_windows(item, windows_index, builts["windows"], counts)
            except Exception as exc:
                failed += 1
                logger.error("Vulnerabilities: re-matching Windows server %s failed: %s", item["server_id"],
                             getattr(exc, "detail", None) or repr(exc))
                continue
            if outcome:
                rematched += 1
                unchanged += outcome == "unchanged"
            _progress(job, "Matched {} of {}".format(done, total), done, total)

    if items and not stopped:
        progress("Matching {} against the vulnerability database".format(_plural(len(items), "server")))
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vuln-rematch")
        waiting = list(items)
        pending = {}
        try:
            while waiting or pending:
                if _cancelled(job):
                    stopped = True
                    break
                if not out_of_time and _clock() - started_at >= JOB_BUDGET_S:
                    # No new server; the matches in flight finish and are saved, then the cleanup runs.
                    out_of_time = True
                    left = len(waiting)
                    waiting = []
                while waiting and len(pending) < workers:
                    item = waiting.pop(0)
                    server, reason = _requalify(item["server_id"])
                    if reason:
                        counts[reason] += 1
                        continue
                    item["server"] = server
                    pending[executor.submit(_match_worker, item)] = item
                if not pending:
                    continue
                finished, _ = wait(list(pending), timeout=WAIT_S, return_when=FIRST_COMPLETED)
                for future in finished:
                    item = pending.pop(future)
                    done += 1
                    try:
                        try:
                            parsed = future.result()
                        except Exception:
                            if _changed_meanwhile(item):
                                counts["changed"] += 1
                                continue
                            raise
                        outcome = _save_rematch(item, parsed, built, counts)
                    except Exception as exc:
                        failed += 1
                        logger.error("Vulnerabilities: re-matching server %s failed: %s", item["server_id"],
                                     getattr(exc, "detail", None) or repr(exc))
                        continue
                    if outcome:
                        rematched += 1
                        unchanged += outcome == "unchanged"
                    _progress(job, "Matched {} of {}".format(done, total), done, total)
        except BaseException:
            # A cancel arrives here: do not wait for the matches in flight.
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        executor.shutdown(wait=True, cancel_futures=True)

    if not stopped:
        # The matches nothing references any more, then the fleet tables rebuilt from the per-server rows.
        for what, call in (("clearing the old matches", vuln_db.prune_orphans),
                           ("rebuilding the fleet totals", vuln_db.rebuild_fleet)):
            try:
                call()
            except Exception as exc:
                logger.exception("Vulnerabilities: %s failed", what)
                notes.append("Afterwards, {} failed: {}".format(what, vuln_jobs.clean(str(exc), 150)))

    skipped = {code: counts[code] for code in counts if counts[code]}
    sources = []
    if "linux" in builts:
        sources.append("the vulnerability database built {}".format(builts["linux"]))
    if "windows" in builts:
        sources.append("the Microsoft security data built {}".format(builts["windows"]))
    parts = ["Re-matched {} against {}".format(_plural(rematched, "server"), " and ".join(sources))]
    if rematched:
        parts[0] += " ({} with no change in findings)".format(unchanged)
    if skipped:
        parts.append("skipped {}".format(_words(skipped)))
    if failed:
        parts.append("{} failed".format(failed))
    if forgotten:
        parts.append("removed what was stored for {}".format(_plural(forgotten, "deleted server")))
    if stopped:
        parts.append("stopped early because the job was canceled")
    if out_of_time:
        parts.append("stopped at the time limit of {} hours, {} left for the next run".format(
            JOB_BUDGET_S // 3600, _plural(left, "server")))
    message = "; ".join(parts) + "."
    if notes:
        message += " " + " ".join(notes)
    outputs = {"rematched": rematched, "unchanged": unchanged, "failed": failed, "forgotten": forgotten,
               "skipped": skipped, "db_built": built}
    if "windows" in builts:
        outputs["windows_built"] = builts["windows"]
    if total and rematched == 0 and failed and not stopped:
        return "FAILURE", message, "No server could be re-matched.", outputs
    outputs["left"] = left
    return ("WARNING" if stopped or out_of_time else "SUCCESS"), message, "", outputs


# ---------------------------------------------------------------- weekly inventory
def run_inventory(job, **kwargs):
    """Plugin entry point (via vuln_inventory_job.py). Returns (status, output, errors, outputs)."""
    try:
        return _inventory(job)
    except Exception as exc:
        logger.exception("Vulnerabilities: the weekly inventory failed")
        return "FAILURE", "", "The weekly inventory failed: {}".format(vuln_jobs.clean(str(exc), 300)), {}


def _recent(moment):
    """True when a store.stamp() value is younger than RECENT_S."""
    age = vuln_jobs._seconds_since(moment) if moment else None
    return age is not None and age < RECENT_S


def _fetch_for_scan(server_id):
    """The Server as candidates() would load it (group, OS family, resource handler, tags), read fresh; None
    when it is gone. In the job's thread."""
    return (Server.objects.filter(id=server_id).select_related("group", "os_family", "resource_handler")
            .prefetch_related("tags").first())


def _run_queue(job, queue, base_url, parallel, profile, check, counts, what, owner_of=None):
    """The bounded-parallel loop of the weekly inventory and of the resource scan: starts a scan (vuln_jobs.start,
    as a child of job, owned by profile or by nobody when profile is None) for each server id of queue in order,
    at most parallel at once, and waits for them. Just before a start the server is read fresh
    (_fetch_for_scan) and check(server) must return "" or a reason code; a reason, a server that is gone, a
    refusal from start and a check that raises are counted in counts (the caller's dict, keyed by REASONS) and
    never end the run; a start that raises counts as failed. Stops starting on a cancel request (_cancelled) and
    at JOB_BUDGET_S, waits for the scans in flight, and writes the job's progress only when the count changes.
    Every ORM call here runs in the job's own thread. what names the caller in the log lines. owner_of, when
    given, is called just before each start (after check passed) and returns the profile to own that scan.
    Returns {"scanned", "failed", "left" (never started because of the budget), "stopped", "out_of_time"}."""
    total = len(queue)
    queue = list(queue)
    scanned = failed = left = skipped = 0
    stopped = out_of_time = False
    started_at = _clock()
    running = {}
    reported = 0  # the count last written to the job, so a long wait does not repeat one line

    while queue or running:
        if _cancelled(job):
            stopped = True
            break
        if not out_of_time and _clock() - started_at >= JOB_BUDGET_S:
            # No new scan; the ones running are waited for, so the job ends before the engine's own limit.
            out_of_time = True
            left = len(queue)
            queue = []
        while queue and len(running) < parallel:
            server_id = queue.pop(0)
            try:
                server = _fetch_for_scan(server_id)
                reason = "gone" if server is None else check(server)
            except Exception:
                logger.exception("Vulnerabilities: could not read server %s again", server_id)
                reason = "unknown"
            if reason:
                counts[reason] += 1
                skipped += 1
                continue
            try:
                started = vuln_jobs.start(owner_of() if owner_of else profile, server, base_url, parent_job=job)
            except Exception:
                # One server that cannot be started (for example an upload link that cannot be made) must not
                # end the weekly run.
                logger.exception("Vulnerabilities: %s could not start a scan of server %s", what, server_id)
                failed += 1
                continue
            started_id = _started_id(started)
            if started_id is None:
                counts["refused"] += 1
                skipped += 1
                logger.info("Vulnerabilities: %s skipped server %s: %s", what, server_id, getattr(started, "message", ""))
            else:
                running[started_id] = server
        if skipped + scanned + failed != reported:
            reported = skipped + scanned + failed
            _progress(job, "{} of {} servers done".format(reported, total), reported, max(total, 1))
        if not running:
            continue
        time.sleep(POLL_S)
        # One query for the status of every scan in flight (the same table jobs.models.get_jobs_status_counts
        # reads, line 79), since a free slot needs to know which one ended and how.
        states = dict(Job.objects.filter(id__in=list(running)).values_list("id", "status"))
        for job_id in list(running):
            state = states.get(job_id)
            if state in Job.COMPLETED_STATUSES:
                scanned += state in ("SUCCESS", "WARNING")
                failed += state not in ("SUCCESS", "WARNING")
                del running[job_id]
            elif state is None:
                failed += 1
                del running[job_id]
        if scanned + failed + skipped != reported:
            reported = scanned + failed + skipped
            _progress(job, "{} of {} servers done".format(reported, total), reported, max(total, 1))
    return {"scanned": scanned, "failed": failed, "left": left, "stopped": stopped, "out_of_time": out_of_time}


def _inventory(job):
    try:
        vuln_db.upgrade()   # also the one-time upgrade of a store the previous release left
    except Exception as exc:
        logger.exception("Vulnerabilities: the findings store could not be opened")
        return "FAILURE", "", _store_unreadable(exc), {}
    base_url = default_base_url()
    problems = prerequisites(base_url)
    if nothing_can_run(problems):
        return "FAILURE", "", not_ready_message(problems), {}
    forgotten = forget_stale(lambda message: _progress(job, message))["forgotten"]

    cfg = store.get_settings()
    visibility = cfg["visibility"]
    parallel = max(1, min(10, int(cfg["inventory_parallel"])))
    last_list = {}
    for row in vuln_db.known_servers():
        last_list[int(row["server_id"])] = row.get("inventory_at")

    counts = {key: 0 for key in REASONS}
    # This pass only builds the ordered queue of ids: the scans start hours later, so each server is read and
    # checked again right before its scan starts.
    queue = []
    for server in candidates(visibility):
        reason = eligibility(server, visibility)
        if not reason:
            # A kind of server whose data is missing is skipped and counted; the other kind still runs.
            reason = prerequisite_for(server, problems)
        if not reason and _recent(last_list.get(server.id)):
            reason = "recent"
        if reason:
            counts[reason] += 1
        else:
            queue.append(server.id)
    # The oldest stored software list first, servers with none before those (the empty string sorts first), so
    # each run carries on where the one before stopped. The sort is stable: ties stay in id order.
    queue.sort(key=lambda server_id: last_list.get(server_id) or "")
    total = len(queue)
    _progress(job, "Scanning {}".format(_plural(total, "server")))

    def check(server):
        return eligibility(server, visibility)

    ran = _run_queue(job, queue, base_url, parallel, None, check, counts, "the weekly inventory")
    scanned, failed, left = ran["scanned"], ran["failed"], ran["left"]
    stopped, out_of_time = ran["stopped"], ran["out_of_time"]

    skipped = {code: counts[code] for code in counts if counts[code]}
    parts = ["Scanned {}".format(_plural(scanned, "server"))]
    if failed:
        parts.append("{} failed".format(failed))
    if skipped:
        parts.append("skipped {}".format(_words(skipped)))
    if forgotten:
        parts.append("removed what was stored for {}".format(_plural(forgotten, "deleted server")))
    if stopped:
        parts.append("stopped early because the job was canceled")
    if out_of_time:
        parts.append("stopped at the time limit of {} hours, {} left for the next run".format(
            JOB_BUDGET_S // 3600, _plural(left, "server")))
    message = "; ".join(parts) + "."
    outputs = {"scanned": scanned, "failed": failed, "skipped": skipped, "forgotten": forgotten, "left": left}
    if total and scanned == 0 and (failed or counts["refused"]) and not stopped:
        return "FAILURE", message, "No server could be scanned.", outputs
    # Something to tell the reader: a stop, the time limit, or a server that failed among those that were scanned.
    return ("WARNING" if stopped or out_of_time or failed else "SUCCESS"), message, "", outputs


# ---------------------------------------------------------------- scan all servers of a resource
RESOURCE_STATE_ERROR = ("The scan state for this resource cannot be read or written; ask an administrator to check "
                        "the Vulnerabilities folder.")


def supported_q():
    """The Q that keeps servers of a Linux or Windows OS family (the family itself or one of its two parents
    up), the same test the Vulnerabilities views use. Q is imported here so this module still imports
    without Django's query layer."""
    from django.db.models import Q
    linux, windows = vuln_jobs.LINUX_FAMILY, "Windows"
    return (Q(os_family__name=linux) | Q(os_family__parent__name=linux)
            | Q(os_family__parent__parent__name=linux) | Q(os_family__name=windows)
            | Q(os_family__parent__name=windows) | Q(os_family__parent__parent__name=windows))


def resource_servers(resource):
    """The resource's own servers that are not historical (sub-resources are not included), with what
    eligibility() reads loaded ahead, ordered by hostname then id."""
    # resources/models.py Resource.server_set: Server.resource is a ForeignKey (infrastructure/models.py line 3377).
    return (resource.server_set.exclude(status="HISTORICAL")
            .select_related("group", "os_family", "resource_handler").prefetch_related("tags")
            .order_by("hostname", "id"))


def scannable_ids(profile, resource, visibility):
    """The ids, in resource_servers() order, of the resource's servers that profile may view and scan and
    that pass eligibility(). One query decides who may view; can_scan is asked for each server."""
    # infrastructure/models.py Server.objects_for_profile (line 3579) returns a plain queryset: never
    # .distinct() on it, and the ids are put in a set instead.
    viewable = set(Server.objects_for_profile(profile).filter(resource=resource).values_list("id", flat=True))
    found = []
    for server in resource_servers(resource):
        if server.id in viewable and vuln_jobs.can_scan(profile, server) and not eligibility(server, visibility):
            found.append(server.id)
    return found


def any_scannable(profile, resource, visibility):
    """True as soon as ONE of the resource's servers is viewable and scannable by profile and passes
    eligibility(); False when none, and on any error. The same rules and the same single
    objects_for_profile query as scannable_ids, but it stops at the first hit."""
    try:
        viewable = set(Server.objects_for_profile(profile).filter(resource=resource).values_list("id", flat=True))
        for server in resource_servers(resource):
            if server.id in viewable and vuln_jobs.can_scan(profile, server) and not eligibility(server, visibility):
                return True
    except Exception:
        logger.exception("Vulnerabilities: could not tell whether resource %s has a scannable server",
                         getattr(resource, "id", None))
    return False


def _live_run(resource_id):
    """(marker, Job) of the scan run in progress on the resource, else None. A marker whose Job is gone or no
    longer active is stale and reads as no run (vuln_jobs._active). Raises store.StoreError when the marker
    file cannot be read."""
    return vuln_jobs._active(store.read_resource_run(resource_id))


def resource_run(resource_id):
    """The run in progress on the resource: {"job_id", "job_url", "started", "done", "total", "kind", "message"}
    (kind "scan", or "patch" for the install-updates run of vuln_patch_resource.py; message is the job's
    progress text), or None when there is none (a stale marker, or one that cannot be read, is no run).
    Never raises."""
    try:
        found = _live_run(resource_id)
        if found is None:
            return None
        marker, job = found
        # jobs/models.py Job.tasks_done / total_tasks (lines 1034-1035, written by set_progress, line 1192),
        # Job.get_absolute_url (line 1679).
        total = job.total_tasks or len(marker.get("server_ids") or [])
        return {"job_id": job.id, "job_url": job.get_absolute_url(), "started": marker.get("started") or "",
                "done": job.tasks_done or 0, "total": total, "kind": marker.get("kind") or "scan",
                "message": vuln_jobs._progress_text(job)}
    except Exception:
        logger.exception("Vulnerabilities: could not read the scan run of resource %s", resource_id)
        return None


def _int_ids(values):
    """The distinct positive ints among values, in order."""
    found = []
    for value in values or ():
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in found:
            found.append(number)
    return found


def start_resource_scan(profile, resource, server_ids, base_url):
    """Start ONE job (run as profile) that scans server_ids of the resource, and record it as the resource's
    run in progress. Returns the Job, or a vuln_jobs.Refusal: 400 when there is nothing to scan, 409 when a run
    is already in progress, the scanner is not set up or base_url is empty, 500 when the state cannot be
    written. The job checks every server again, as profile, just before it starts it."""
    ids = _int_ids(server_ids)
    if not ids:
        return vuln_jobs.Refusal("There is no server to scan.", 400)
    try:
        with store.resource_lock(resource.id):
            running = _live_run(resource.id)
            if running is not None:
                doing = "installing updates on" if running[0].get("kind") == "patch" else "scanning"
                return vuln_jobs.Refusal(
                    "Job {} is already {} the servers of this resource.".format(running[1].id, doing), 409)
            problems = prerequisites(base_url)
            if nothing_can_run(problems):
                return vuln_jobs.Refusal(not_ready_message(problems), 409)
            requested_by = vuln_jobs._requested_by(profile)
            created = vuln_hooks.resource_scan_hook().run_as_job(
                owner=profile,
                parent_job=None,
                context={"vuln_resource_id": resource.id, "vuln_server_ids": ids, "vuln_requested_by": requested_by},
            )
            if not created:
                return vuln_jobs.Refusal("CloudBolt did not start the job; see the application log.", 500)
            job = created[0]
            # From here the job is queued: nothing below may turn into an error.
            try:
                job.label = "Vulnerability scan: {}".format(resource.name)
                job.save(update_fields=["label"])
                # resources/models.py Resource.jobs (line 194): the job shows on the resource's own Jobs tab.
                resource.jobs.add(job)
            except Exception:
                logger.exception("Vulnerabilities: could not label or link job %s", job.id)
            try:
                store.set_resource_run(resource.id, {"job_id": job.id, "started": store.stamp(),
                                                     "requested_by": requested_by, "server_ids": ids})
            except Exception:
                logger.exception("Vulnerabilities: could not record job %s as the run on resource %s", job.id,
                                 resource.id)
            return job
    except store.StoreError:
        logger.exception("Vulnerabilities: state problem starting a scan of resource %s", resource.id)
        return vuln_jobs.Refusal(RESOURCE_STATE_ERROR, 500)


def run_resource_scan(job, vuln_resource_id=None, vuln_server_ids=None, vuln_requested_by="", **kwargs):
    """Plugin entry point (via vuln_resource_job.py). Returns (status, output, errors, outputs). The job's
    marker is written again here and always cleared at the end, for this job only, whatever way it ends."""
    try:
        resource_id = int(vuln_resource_id)
    except (TypeError, ValueError):
        return "FAILURE", "", "This job was not given a resource to scan.", {}
    try:
        _record_run(job, resource_id, vuln_server_ids, vuln_requested_by)
        return _resource_scan(job, resource_id, vuln_server_ids)
    except Exception as exc:
        logger.exception("Vulnerabilities: the scan of resource %s failed", resource_id)
        return "FAILURE", "", "The scan of the resource's servers failed: {}".format(
            vuln_jobs.clean(str(exc), 300)), {}
    finally:
        # Runs for a cancel too (a BaseException passes the except above untouched).
        try:
            store.clear_resource_run(resource_id, job_id=job.id)
        except Exception:
            logger.exception("Vulnerabilities: could not clear the scan run of resource %s", resource_id)


def _record_run(job, resource_id, server_ids, requested_by):
    """Write the marker, unless another job that is still active holds it (a rerun of an old job must not
    hide the newer run). Never raises: the marker is advisory."""
    try:
        with store.resource_lock(resource_id):
            found = _live_run(resource_id)
            if found is not None and found[1].id != job.id:
                return
            store.set_resource_run(resource_id, {"job_id": job.id, "started": store.stamp(),
                                                 "requested_by": requested_by, "server_ids": _int_ids(server_ids)})
    except Exception:
        logger.exception("Vulnerabilities: could not record job %s as the run on resource %s", job.id, resource_id)


def _resource_scan(job, resource_id, server_ids):
    owner = getattr(job, "owner", None)
    if owner is None:
        # A job that is rerun or cloned can lose its owner. Nobody's permissions can be checked then, so
        # nothing may be scanned.
        message = ("This scan has no owner, so nothing was scanned. Start it again from the resource's "
                   "Vulnerabilities tab.")
        return "FAILURE", message, message, {}
    queue = _int_ids(server_ids)
    if not queue:
        return "FAILURE", "", "This job was given no server to scan.", {}
    base_url = default_base_url()
    problems = prerequisites(base_url)
    if nothing_can_run(problems):
        return "FAILURE", "", not_ready_message(problems), {}
    cfg = store.get_settings()
    visibility = cfg["visibility"]
    parallel = max(1, min(10, int(cfg["inventory_parallel"])))
    counts = {key: 0 for key in REASONS}
    # The owner as last read; replaced before every permission check, because the job may run for hours.
    state = {"profile": owner, "revoked": False}

    def check(server):
        # Read fresh just now: the request that started the job may be hours old.
        if getattr(server, "resource_id", None) != resource_id:
            return "left_resource"
        if server.status == "HISTORICAL":
            return "historical"
        if vuln_jobs.in_unassigned_group(server):
            return "unassigned"
        # The owner's roles are read off the loaded profile (accounts/models.py UserProfile.is_cbadmin, line 1380),
        # and User.is_active is never consulted by them, so read both again here: one query by primary key
        # (accounts/models.py UserProfile.user, line 558; jobs/models.py Job.owner, line 1012). A profile that is
        # gone or a user that is no longer active has no permission, for this and every remaining server.
        if state["revoked"]:
            return "no_permission"
        fresh = UserProfile.objects.select_related("user").filter(id=owner.id).first()
        user = getattr(fresh, "user", None)
        if fresh is None or user is None or not user.is_active:
            state["revoked"] = True
            return "no_permission"
        state["profile"] = fresh
        # As the person who started the scan: view (Server.objects_for_profile) and scan (server.all_actions).
        if not (vuln_jobs.can_view(fresh, server) and vuln_jobs.can_scan(fresh, server)):
            return "no_permission"
        return eligibility(server, visibility) or prerequisite_for(server, problems)

    total = len(queue)
    _progress(job, "Scanning {}".format(_plural(total, "server")), 0, total)
    ran = _run_queue(job, queue, base_url, parallel, owner, check, counts, "the resource scan",
                     owner_of=lambda: state["profile"])
    scanned, failed, left = ran["scanned"], ran["failed"], ran["left"]
    stopped, out_of_time = ran["stopped"], ran["out_of_time"]

    skipped = {code: counts[code] for code in counts if counts[code]}
    parts = ["Scanned {}".format(_plural(scanned, "server"))]
    if failed:
        parts.append("{} failed".format(failed))
    if skipped:
        parts.append("skipped {}".format(_words(skipped)))
    if stopped:
        parts.append("stopped early because the job was canceled")
    if out_of_time:
        parts.append("stopped at the time limit of {} hours, {} not started".format(
            JOB_BUDGET_S // 3600, _plural(left, "server")))
    message = "; ".join(parts) + "."
    outputs = {"scanned": scanned, "failed": failed, "skipped": skipped, "left": left}
    if scanned == 0 and not stopped:
        return "FAILURE", message, "No server could be scanned.", outputs
    return ("WARNING" if stopped or out_of_time or failed else "SUCCESS"), message, "", outputs


# ---------------------------------------------------------------- scan after provisioning
def run_provision(job, **kwargs):
    """Plugin entry point (via vuln_provision_job.py), run at the Post-Provision hook point inside every
    provisioning job once the setting is on. Cheap, never raises (a cancel still propagates), always SUCCESS."""
    try:
        message = _provision(job, kwargs)
    except Exception:
        logger.exception("Vulnerabilities: the scan after provisioning could not start")
        message = "No scan was started; the CloudBolt application log has the details."
    return "SUCCESS", message, "", {}


def _provision(job, kwargs):
    # The status the job had after its own work: _run_post_job_hooks (jobs/models.py line 2273) keeps it on
    # the job in memory while the post-job actions run.
    if getattr(job, "status", "") in BAD_STATUSES:
        return "No scan was started because the build did not finish."
    cfg = store.get_settings()
    if not cfg["scan_on_provision"]:
        return "Scanning new servers is turned off."
    base_url = default_base_url()
    problems = prerequisites(base_url)
    if nothing_can_run(problems):
        if problems[0] == "not_set_up":
            return "No scan was started: the scanner is not set up yet."
        return "No scan was started: " + vuln_jobs.NO_SITE_URL
    # Hook-point actions get the job; its servers are the ones the build made (cbhooks/models.py line 4770
    # looks for servers in kwargs, the context and the job the same way).
    servers = list(kwargs.get("servers") or (job.server_set.all() if job is not None else []))
    if not servers:
        return "No scan was started: the build made no server."
    started, skipped = [], {}
    for server in servers:
        reason = eligibility(server, cfg["visibility"]) or prerequisite_for(server, problems)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        # parent_job=None on purpose: the scan is an independent job, so the build neither waits for it
        # nor is cancelled with it (cbhooks/models.py run_as_job, line 1420).
        try:
            created = vuln_jobs.start(getattr(job, "owner", None), server, base_url, parent_job=None)
        except Exception:
            # One server that cannot be started must not stop the others of the same build.
            logger.exception("Vulnerabilities: the scan of new server %s could not start", server.id)
            skipped["failed"] = skipped.get("failed", 0) + 1
            continue
        created_id = _started_id(created)
        if created_id is None:
            skipped["refused"] = skipped.get("refused", 0) + 1
            logger.info("Vulnerabilities: no scan of new server %s: %s", server.id, getattr(created, "message", ""))
        else:
            started.append("{} (job {})".format(server.hostname, created_id))
    parts = []
    if started:
        parts.append("Started a vulnerability scan of {}".format(", ".join(started)))
    if skipped:
        parts.append("{} {}".format("Skipped" if started else "No scan was started. Skipped", _words(skipped)))
    return ". ".join(parts) + "."
