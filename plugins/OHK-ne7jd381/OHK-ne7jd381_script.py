"""Post-Provision action: installs OpenSCAP on each new Linux server and runs a first scan.

All of the scan work belongs to the Compliance UI extension (extensions/XUI-i4kzuy3y). This action
asks its oscap_api which servers of the job may be scanned, then calls oscap_api.scan_and_wait for each
one: that installs OpenSCAP when it is missing, runs the scan as its own job and adds the result to the
history on the server's Compliance tab. Servers are scanned one after another, and provisioning waits,
up to the maximum wait for the whole action; servers left when the time is up are skipped.

It never fails or warns provisioning. Every problem is written to the job's progress messages, and the
action always returns SUCCESS.

Inputs: profile (a profile id or its ending, such as cis_server_l1; empty picks the first default
profile of the Compliance settings page that the server's guide offers), max_wait_minutes (how long
provisioning may wait for all of the job's scans together; empty uses 30).
"""

import time

from common.methods import set_progress


def _clean(value):
    """A plug-in input as text: an input left empty renders as an empty string or the word None."""
    value = (value or "").strip()
    return "" if value in ("", "None") else value


PROFILE = _clean("{{ profile }}")
MAX_WAIT = _clean("{{ max_wait_minutes }}")


def _budget_seconds():
    try:
        minutes = int(MAX_WAIT)
    except ValueError:
        minutes = 30
    return (minutes if 1 <= minutes <= 240 else 30) * 60


def _scan(oscap_api, server, owner, timeout):
    """Scan one server; returns the line for the job output."""
    host = server.hostname
    set_progress(f"{host}: installing OpenSCAP if it is missing and scanning")
    result = oscap_api.scan_and_wait(server, owner, profile_id=PROFILE, install=True, timeout=timeout)
    if result["job"] is not None:
        set_progress(f"{host}: the scan ran as job {result['job'].id}")
    message = result["message"]
    if result["status"] != "completed":
        return message if host in message else f"{host}: no first scan, {message}"
    if result["report_url"]:
        set_progress(f"{host}: report {result['report_url']}")
    else:
        set_progress(f"{host}: no report link, {result['report_problem']}")
    return message


def _one(oscap_api, server, owner, deadline):
    """The outcome line for one server; never raises."""
    host = getattr(server, "hostname", "server")
    try:
        if server.is_windows():
            return f"{host}: skipped, this action scans Linux servers."
        ok, reason = oscap_api.eligible(server)
        if not ok:
            return f"{host}: skipped, {reason}"
        left = int(deadline - time.monotonic())
        if left < 60:
            return f"{host}: skipped, the maximum wait of this action was used up by earlier servers."
        return _scan(oscap_api, server, owner, left)
    except Exception as err:
        return f"{host}: no first scan, {err}"


def run(job, *args, **kwargs):
    try:
        if job.status == "FAILURE":
            return "SUCCESS", "Provisioning failed, so no server was scanned.", ""
        try:
            from xui.io_cloudbolt_compliance import oscap_api

            oscap_api.eligible
        except ImportError:
            set_progress("The Compliance extension (extensions/XUI-i4kzuy3y) is not installed, so no server was scanned.")
            return "SUCCESS", "", ""
        except AttributeError:
            set_progress("The installed Compliance extension is too old to offer eligible(); sync extensions/XUI-i4kzuy3y and restart the web service.")
            return "SUCCESS", "", ""
        deadline = time.monotonic() + _budget_seconds()
        lines = []
        for server in kwargs.get("servers") or list(job.server_set.all()):
            line = _one(oscap_api, server, job.owner, deadline)
            set_progress(line)
            lines.append(line)
        return "SUCCESS", "\n".join(lines) or "No eligible Linux server in this job.", ""
    except Exception as err:
        set_progress(f"The first OpenSCAP scan stopped early: {err}")
        return "SUCCESS", "", ""
