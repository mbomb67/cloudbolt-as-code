"""Post-Provision action: installs SCC on each new Windows server and starts a first scan.

All of the scan work belongs to the Compliance UI extension (extensions/XUI-i4kzuy3y). This action
asks its oscap_api which servers of the job may be scanned, then calls oscap_api.start_scan for each
one: that installs SCC when it is missing and runs the scan as its own job, a sub-job of the
provisioning job. The action does not wait for the scan, so the server is handed over while the scan
runs. The result and the report appear on the server's Compliance tab when the scan's job ends.

It never fails or warns provisioning. Every problem is written to the job's progress messages, and the
action always returns SUCCESS.

Inputs: profile (an SCC profile id, such as MAC-1_Classified; empty uses the Windows default profile
of the Compliance settings page).
"""

import importlib

from common.methods import set_progress

PACKAGE = "xui.io_cloudbolt_compliance"


def _clean(value):
    """A plug-in input as text: an input left empty renders as an empty string or the word None."""
    value = (value or "").strip()
    return "" if value in ("", "None") else value


PROFILE = _clean("{{ profile }}")


def _load_api():
    """The extension's oscap_api, freshly loaded. The job engine keeps modules in memory, so the
    extension's modules are reloaded the way its own scan job does it (oscap_job.py): a synced
    extension update then counts without a restart."""
    entry = importlib.reload(importlib.import_module(PACKAGE + ".oscap_job"))
    for name in tuple(entry.MODULES) + (PACKAGE + ".oscap_api",):
        importlib.reload(importlib.import_module(name))
    return importlib.import_module(PACKAGE + ".oscap_api")


def _one(oscap_api, server, job):
    """The outcome line for one server; never raises."""
    host = getattr(server, "hostname", "server")
    try:
        if not server.is_windows():
            return f"{host}: skipped, this action scans Windows servers."
        ok, reason = oscap_api.eligible(server)
        if not ok:
            return f"{host}: skipped, {reason}"
        set_progress(f"{host}: starting the scan, SCC is installed first if it is missing")
        result = oscap_api.start_scan(server, job.owner, profile_id=PROFILE, install=True, parent_job=job)
        message = result["message"]
        if result["status"] == "started":
            return f"{host}: scan started as job {result['job'].id}; the result will be on the server's Compliance tab."
        return message if host in message else f"{host}: no first scan, {message}"
    except Exception as err:
        return f"{host}: no first scan, {err}"


def run(job, *args, **kwargs):
    try:
        if job.status == "FAILURE":
            return "SUCCESS", "Provisioning failed, so no server was scanned.", ""
        try:
            oscap_api = _load_api()
            oscap_api.start_scan
        except ImportError:
            set_progress("The Compliance extension (extensions/XUI-i4kzuy3y) is not installed, so no server was scanned.")
            return "SUCCESS", "", ""
        except AttributeError:
            set_progress("The installed Compliance extension is too old to offer start_scan(); sync extensions/XUI-i4kzuy3y.")
            return "SUCCESS", "", ""
        lines = []
        for server in kwargs.get("servers") or list(job.server_set.all()):
            line = _one(oscap_api, server, job)
            set_progress(line)
            lines.append(line)
        return "SUCCESS", "\n".join(lines) or "No eligible Windows server in this job.", ""
    except Exception as err:
        set_progress(f"The first SCC scan could not be started: {err}")
        return "SUCCESS", "", ""
