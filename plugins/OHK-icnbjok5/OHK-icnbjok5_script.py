"""Blueprint build item: starts a compliance scan of every server of the deployment.

Add it as the last build item of a blueprint, after the scripts and installs that can change what a
scan finds. CloudBolt hands a plug-in build item every server of the deployed resource (the servers
argument of run), and this step starts one scan for each: OpenSCAP on Linux, DISA SCC on Windows.

All of the scan work belongs to the Compliance UI extension (extensions/XUI-i4kzuy3y). This step asks
its oscap_api which servers may be scanned, then calls oscap_api.start_scan for each one: that installs
the scanner when it is missing and runs the scan as its own job, a sub-job of this step's job. The step
does not wait for the scans, so the order completes while they run. Each result and report appears on
the server's Compliance tab, and on the resource's Compliance tab, when its scan ends.

The Post-Provision first-scan actions (HPA-b14wu721, HPA-ruxgbj67) skip a server whose blueprint runs
more build items after its tier, so a blueprint that ends with this step is scanned once, here.

Inputs: linux_profile (an OpenSCAP profile id or its ending, such as cis_server_l1) and windows_profile
(an SCC profile id, such as MAC-1_Classified). An empty input uses the default profile of the
Compliance settings page.

Returns FAILURE when the Compliance extension is missing or too old, WARNING when a scan could not be
started on a server that should be scanned, and SUCCESS otherwise.
"""

import importlib

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

PACKAGE = "xui.io_cloudbolt_compliance"
NO_EXTENSION = (
    "The Compliance UI extension is not installed on this CloudBolt, or is too old to start a scan "
    "from a plug-in. Sync extensions/XUI-i4kzuy3y, restart the web service, and run this step again."
)


def _clean(value):
    """A plug-in input as text: an input left empty renders as an empty string or the word None."""
    value = (value or "").strip()
    return "" if value in ("", "None") else value


LINUX_PROFILE = _clean("{{ linux_profile }}")
WINDOWS_PROFILE = _clean("{{ windows_profile }}")


def _load_api():
    """The extension's oscap_api, freshly loaded. The job engine keeps modules in memory, so the
    extension's modules are reloaded the way its own scan job does it (oscap_job.py): a synced
    extension update then counts without a restart."""
    try:
        entry = importlib.reload(importlib.import_module(PACKAGE + ".oscap_job"))
        for name in tuple(entry.MODULES) + (PACKAGE + ".oscap_api",):
            importlib.reload(importlib.import_module(name))
    except ImportError:
        raise
    except Exception as err:
        logger.warning(f"Compliance scan step: the extension could not be reloaded ({type(err).__name__}: {err}); using the copy in memory.")
    return importlib.import_module(PACKAGE + ".oscap_api")


def _one(oscap_api, server, job):
    """("started" | "skipped" | "problem", line) for one server; never raises."""
    host = getattr(server, "hostname", "server")
    try:
        ok, reason = oscap_api.eligible(server)
        if not ok:
            return "skipped", f"{host}: skipped, {reason}"
        profile = WINDOWS_PROFILE if server.is_windows() else LINUX_PROFILE
        result = oscap_api.start_scan(server, job.owner, profile_id=profile, install=True, parent_job=job)
        if result["status"] == "started":
            return "started", f"{host}: scan started as job {result['job'].id}."
        message = result["message"]
        return "problem", message if host in message else f"{host}: no scan, {message}"
    except Exception as err:
        logger.exception(f"Compliance scan step: the scan of {host} could not be started")
        return "problem", f"{host}: no scan, {err}"


def run(job, servers=None, resource=None, **kwargs):
    try:
        oscap_api = _load_api()
    except ImportError:
        return "FAILURE", "", NO_EXTENSION
    if not hasattr(oscap_api, "start_scan"):
        return "FAILURE", "", NO_EXTENSION

    # A plug-in build item gets every server of the deployed resource as servers
    # (cbhooks/models.py OrchestrationHook.run_as_job); the job and the resource hold the same set.
    servers = list(servers or job.server_set.all())
    if not servers and resource is not None:
        servers = list(resource.server_set.all())
    if not servers:
        return "SUCCESS", "This deployment has no servers to scan.", ""

    started, problems = [], []
    for server in servers:
        outcome, line = _one(oscap_api, server, job)
        set_progress(line)
        if outcome == "started":
            started.append(line)
        elif outcome == "problem":
            problems.append(line)

    where = "the Compliance tab of each server"
    if resource is not None:
        where += " and of {}".format(resource.name)
    summary = "Started {} of {} compliance scans; the order does not wait for them. Results will be on {}.".format(
        len(started), len(servers), where
    )
    if problems:
        return "WARNING", summary, "\n".join(problems)
    return "SUCCESS", summary, ""
