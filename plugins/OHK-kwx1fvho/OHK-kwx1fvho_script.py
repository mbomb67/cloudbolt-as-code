"""Post-Provision action: installs SCC on each new Windows server and starts a first scan.

All of the scan work belongs to the Compliance UI extension (extensions/XUI-i4kzuy3y). This action
asks its oscap_api which servers of the job may be scanned, then calls oscap_api.start_scan for each
one: that installs SCC when it is missing and runs the scan as its own job, a sub-job of the
provisioning job. The action does not wait for the scan, so the server is handed over while the scan
runs. The result and the report appear on the server's Compliance tab when the scan's job ends.

A server is skipped when its blueprint runs other build items after the server's tier (plug-ins,
scripts, installs; anything that is not a server tier), because those steps can change what a scan
finds. Such a blueprint ends with the Scan All Servers for Compliance build item (plugins/OHK-icnbjok5).

It never fails or warns provisioning, and always returns SUCCESS. The only progress message it writes
is the line that says a scan started. A skipped server, a refusal and every problem go to the job's log
only, so nothing that reads like an error shows on the provisioning job's page.

Inputs: profile (an SCC profile id, such as MAC-1_Classified; empty uses the Windows default profile
of the Compliance settings page).
"""

import importlib
import os
import time

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

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
    try:
        entry = importlib.reload(importlib.import_module(PACKAGE + ".oscap_job"))
        for name in tuple(entry.MODULES) + (PACKAGE + ".oscap_api",):
            importlib.reload(importlib.import_module(name))
    except ImportError:
        raise
    except Exception as err:
        logger.warning(f"First compliance scan: the extension could not be reloaded ({type(err).__name__}: {err}); using the copy in memory.")
    return importlib.import_module(PACKAGE + ".oscap_api")


def _installed(module):
    """Where the module was loaded from and when that file last changed, for the too-old message."""
    path = getattr(module, "__file__", "") or ""
    try:
        with open(path, encoding="utf-8") as handle:
            on_disk = "def start_scan" in handle.read()
        changed = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
    except OSError as err:
        return f"{path or 'no file'}: {err}"
    return f"{path}, changed {changed}, start_scan {'is' if on_disk else 'is not'} in that file"


def _one(oscap_api, server, job):
    """(True, line) when a scan started on the server, else (False, why not); never raises."""
    host = getattr(server, "hostname", "server")
    try:
        if not server.is_windows():
            return False, f"{host}: skipped, this action scans Windows servers."
        ok, reason = oscap_api.eligible(server)
        if not ok:
            return False, f"{host}: skipped, {reason}"
        # Build items that run after this server's tier can change what a scan finds, so the scan
        # is left to the blueprint's last step. An older extension has no later_build_items.
        later = getattr(oscap_api, "later_build_items", lambda _server, _job=None: [])(server, job)
        if later:
            shown = ", ".join(later[:3]) + (f" and {len(later) - 3} more" if len(later) > 3 else "")
            return False, (
                f"{host}: skipped, its blueprint runs build items after this server's tier ({shown}). "
                "End the blueprint with the Scan All Servers for Compliance build item to scan it then."
            )
        result = oscap_api.start_scan(server, job.owner, profile_id=PROFILE, install=True, parent_job=job)
        if result["status"] == "started":
            return True, f"{host}: scan started as job {result['job'].id}; the result will be on the server's Compliance tab."
        message = result["message"]
        return False, message if host in message else f"{host}: no first scan, {message}"
    except Exception as err:
        return False, f"{host}: no first scan, {err}"


def run(job, *args, **kwargs):
    try:
        if job.status == "FAILURE":
            logger.info("First compliance scan: provisioning failed, so no server was scanned.")
            return "SUCCESS", "", ""
        try:
            oscap_api = _load_api()
        except ImportError:
            logger.warning("First compliance scan: the Compliance extension (extensions/XUI-i4kzuy3y) is not installed, so no server was scanned.")
            return "SUCCESS", "", ""
        if not hasattr(oscap_api, "start_scan"):
            logger.warning(
                "First compliance scan: the installed Compliance extension is too old to offer start_scan(); "
                f"sync extensions/XUI-i4kzuy3y. Loaded from {_installed(oscap_api)}."
            )
            return "SUCCESS", "", ""
        started = []
        for server in kwargs.get("servers") or list(job.server_set.all()):
            ok, line = _one(oscap_api, server, job)
            if ok:
                set_progress(line)
                started.append(line)
            else:
                logger.warning(f"First compliance scan: {line}")
        return "SUCCESS", "\n".join(started), ""
    except Exception as err:
        logger.warning(f"First compliance scan: the SCC scan could not be started: {err}")
        return "SUCCESS", "", ""
