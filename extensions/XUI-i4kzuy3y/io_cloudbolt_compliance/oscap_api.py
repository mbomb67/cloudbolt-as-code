"""
Compliance scans for other content: scan a server from a plug-in, and wait for the result or not.

    from xui.io_cloudbolt_compliance import oscap_api

    result = oscap_api.scan_and_wait(server, job.owner, profile_id="cis_server_l1", install=True)
    result["status"]         "completed", "failed", "refused" or "timeout"
    result["message"]        one sentence for the job output
    result["report_url"]     link to the stored HTML report, or ""
    result["report_problem"] why there is no link, when there is none
    result["scan"]           the scan summary the Compliance tab shows (score, counts, failed rules), or None
    result["job"]            the scan's own Job, or None

The scan is the job the Compliance tab starts (oscap_jobs.start), so it takes the
same lock, lands in the server's scan history and stores its files the same
way.

    result = oscap_api.start_scan(server, job.owner, profile_id="cis_server_l1", install=True, parent_job=job)
    result["status"]         "started" or "refused"
    result["job"]            the scan's own Job when it started

start_scan starts the same job and returns at once. With parent_job the scan is a
sub-job of that job: shown under it, cancelled with it, and the parent neither
waits for it nor is changed by its result. The result is in the server's scan history.

    ok, reason = eligible(server)   (True, "") or (False, one sentence why not)

eligible says whether content may start a scan on a server at all: it is not
historical, has a VM, and gets the Compliance tab under the "Show the tab on"
setting. The Post-Provision actions HPA-b14wu721 (Linux) and HPA-ruxgbj67 (Windows) use eligible
and start_scan.

Nothing here checks a permission: the caller is content an administrator put
in place, running as a job. Do not call it from a web request.
"""
from django.urls import NoReverseMatch, reverse

from utilities.exceptions import TimeoutException
from utilities.logger import ThreadLogger

from . import oscap_jobs, oscap_repo, oscap_store as store

logger = ThreadLogger(__name__)

# Added to the scan timeout setting: queueing, the upload or collection of the files.
WAIT_MARGIN_SECONDS = 600
NO_SITE_URL = "The default portal has no domain (Admin > Portals), so a link to the report cannot be made."


def site_url():
    """The default portal's configured site URL, or "" when it has no domain."""
    from portals.models import PortalConfig

    # With no request this is the default portal (portals/models.py get_current_portal).
    portal = PortalConfig.get_current_portal()
    return ((portal.site_url if portal else None) or "").rstrip("/")


def report_url(server, scan, base=None):
    """An absolute link to a scan's stored HTML report, or "" when it was not
    stored or the portal has no domain. Opening it needs a CloudBolt login and
    the view permission on the server."""
    if not scan or not (scan.get("files") or {}).get(oscap_repo.FILES["report"]):
        return ""
    base = site_url() if base is None else base
    if not base:
        return ""
    try:
        return base + reverse("compliance_artifact", args=[server.id, scan["id"], "report"])
    except NoReverseMatch:
        logger.exception("Compliance: the report route is not loaded")
        return ""


def _result(status, message, job=None, scan=None, url="", problem=""):
    return {
        "status": status, "message": message, "job": job, "scan": scan,
        "report_url": url, "report_problem": problem,
    }


def eligible(server):
    """(True, "") when content may start a scan on this server, else (False, reason)."""
    if server.status == "HISTORICAL":
        return False, "the server is historical."
    if not server.resource_handler_svr_id:
        return False, "the VM was not created."
    if not oscap_jobs.shows_tab(server, store.get_settings()["visibility"]):
        return False, 'the Compliance "Show the tab on" setting does not include this server.'
    return True, ""


def start_scan(server, owner, profile_id="", datastream="", install=False, parent_job=None):
    """Start a scan of one server and return without waiting for it.

    owner is the UserProfile the scan job belongs to (a plug-in passes job.owner).
    profile_id is a profile id or its ending, such as cis_server_l1; empty tries
    the default profiles of the settings page (a Windows server uses the Windows
    default profile). datastream is the SCC benchmark on Windows. install=True
    installs OpenSCAP and the SCAP Security Guide (SCC on Windows) when they are
    missing, whatever the Allow install setting says: that setting governs the
    tab's Install box. parent_job makes the scan a sub-job of that job, which is
    then shown under it and is cancelled with it; the parent does not wait for
    the scan and is not changed by its result. The result appears in the scan
    history on the server's Compliance tab.
    """
    started = oscap_jobs.start(
        owner, server, "scan", datastream, profile_id, False,
        base_url=site_url(), content_install=bool(install), parent_job=parent_job,
    )
    if isinstance(started, oscap_jobs.Refusal):
        return _result("refused", started.message)
    return _result("started", "The scan of {} started as job {}.".format(server.hostname, started.id), job=started)


def scan_and_wait(server, owner, profile_id="", datastream="", install=False, timeout=None):
    """Scan one server and wait for the scan's job to end.

    owner is the UserProfile the scan job belongs to (a plug-in passes
    job.owner). profile_id is a profile id or its ending, such as
    cis_server_l1; empty tries the default profiles of the settings page (a Windows
    server uses the Windows default profile). datastream is the SCC benchmark on Windows.
    install=True installs OpenSCAP and the SCAP Security Guide (SCC on Windows) when they are
    missing, whatever the Allow install setting says: that setting governs the
    tab's Install box. timeout is in seconds; the default is the scan timeout
    setting plus ten minutes.
    """
    base = site_url()
    started = oscap_jobs.start(
        owner, server, "scan", datastream, profile_id, False, base_url=base, content_install=bool(install)
    )
    if isinstance(started, oscap_jobs.Refusal):
        return _result("refused", started.message)

    wait = timeout or store.get_settings()["scan_timeout_min"] * 60 + WAIT_MARGIN_SECONDS
    try:
        # Polls the job every five seconds; cancels it and raises when the time is up
        # (jobs/models.py Job.wait_for_completion).
        job = started.wait_for_completion(timeout=wait)
    except TimeoutException:
        return _result(
            "timeout", "The scan of {} did not finish within {} minutes (job {}).".format(
                server.hostname, wait // 60, started.id), job=started,
        )

    scan = next((s for s in store.read_index(server.id)["scans"] if s.get("job_id") == job.id), None)
    if scan is None:
        detail = oscap_jobs.clean(job.errors or "", oscap_jobs.MESSAGE_LEN)
        return _result(
            "failed", "The scan of {} recorded no result (job {} ended as {}). {}".format(
                server.hostname, job.id, job.status, detail).strip(), job=job,
        )
    if scan.get("status") != "completed":
        return _result(
            "failed", "The scan of {} failed: {}".format(server.hostname, scan.get("error") or "no reason given"),
            job=job, scan=scan,
        )

    score = "{:.1f}%".format(scan["score"]) if scan.get("score") is not None else "no score"
    counts = scan.get("counts") or {}
    message = "{} scanned with {}: {}, {} rules failed, {} passed.".format(
        server.hostname, (scan.get("profile_id") or "the default profile").rsplit("_profile_", 1)[-1], score,
        counts.get("fail", 0), counts.get("pass", 0),
    )
    url = report_url(server, scan, base)
    problem = ""
    if not url:
        stored = (scan.get("files") or {}).get(oscap_repo.FILES["report"])
        problem = NO_SITE_URL if stored else (scan.get("store_problem") or "The report was not stored.")
    return _result("completed", message, job=job, scan=scan, url=url, problem=problem)
