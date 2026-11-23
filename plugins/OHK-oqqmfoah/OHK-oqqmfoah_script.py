"""Blueprint build item: scans a server tier with OpenSCAP and links the report on the resource.

All of the scan work belongs to the Compliance UI extension (extensions/XUI-i4kzuy3y). This step
calls its oscap_api.scan_and_wait, which installs OpenSCAP when it is missing, runs the scan as its
own job, stores the result files and adds the scan to the history on the server's Compliance tab.
The step waits for that job, so the order is not complete until the scan is. It then stores a link
to the HTML report on the resource, one parameter per server (openscap_report_ plus the tier name).

Inputs: tier (a server build item's name; empty scans every server of the resource), profile (a
profile id or its ending, such as cis_server_l1; empty tries the extension's default profiles).
"""

import re

from common.methods import set_progress
from shared_modules import vm_tier_scripts as tiers

TIER = tiers.clean("{{ tier }}")
PROFILE = tiers.clean("{{ profile }}")

NO_EXTENSION = (
    "The Compliance UI extension is not installed on this CloudBolt. Sync extensions/XUI-i4kzuy3y, "
    "restart the web service, and run this step again."
)


def report_parameter(server, position):
    """(name, label) of the resource parameter that holds a server's report link."""
    tier = server.service_item.name if server.service_item else server.hostname
    suffix = "" if position == 1 else f" {position}"
    name = "openscap_report_" + re.sub(r"[^a-z0-9]+", "_", (tier + suffix).lower()).strip("_")
    return name, f"OpenSCAP Report: {tier}{suffix}"


def run(job, resource=None, **kwargs):
    try:
        from xui.io_cloudbolt_compliance import oscap_api
    except ImportError:
        return "FAILURE", "", NO_EXTENSION
    if resource is None:
        return "FAILURE", "", "This step runs as a blueprint build item and found no resource to work on."
    if TIER:
        servers = tiers.tier_servers(resource, TIER)
    else:
        servers = list(resource.server_set.filter(status="ACTIVE").order_by("hostname"))
    if not servers:
        where = f"from a build item named '{TIER}'" if TIER else "to scan"
        return "FAILURE", "", f"The resource has no active server {where}."

    done, failed, unlinked = [], [], []
    for position, server in enumerate(servers, 1):
        set_progress(f"Scanning {server.hostname} with OpenSCAP; installing it first if it is missing")
        result = oscap_api.scan_and_wait(server, job.owner, profile_id=PROFILE, install=True)
        if result["job"] is not None:
            set_progress(f"The scan of {server.hostname} ran as job {result['job'].id}")
        if result["status"] != "completed":
            failed.append(result["message"])
            continue
        done.append(result["message"])
        if result["report_url"]:
            name, label = report_parameter(server, position)
            tiers.remember(
                resource, name, result["report_url"], label, "URL", show=True,
                description="Link to the HTML report of the OpenSCAP scan run when this server was built.",
            )
            set_progress(f"{label}: {result['report_url']}")
        else:
            unlinked.append(f"{server.hostname}: {result['report_problem']}")

    output = "\n".join(done)
    if failed:
        return "FAILURE", output, "\n".join(failed + unlinked)
    if unlinked:
        return "WARNING", output, "Scanned, but no report link was stored. " + "\n".join(unlinked)
    return "SUCCESS", output, ""
