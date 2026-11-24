"""
Vulnerability Scan After Provisioning

The Vulnerabilities UI extension registers this file as a CloudBolt plugin when an administrator turns on the scan after provisioning setting,
then runs it as a job. The work itself lives in the extension (vuln_fleet.py). This
file reloads the extension's modules, so a job picks up an extension update without
a job engine restart, and hands over. Syncing the extension replaces this file; do
not edit it on the appliance.
"""
import importlib

# Reloaded in dependency order: each module imports the ones before it.
MODULES = (
    "xui.io_cloudbolt_vulnerabilities.vuln_store",
    "xui.io_cloudbolt_vulnerabilities.vuln_match",
    "xui.io_cloudbolt_vulnerabilities.vuln_db",
    "xui.io_cloudbolt_vulnerabilities.vuln_tools",
    "xui.io_cloudbolt_vulnerabilities.vuln_repo",
    "xui.io_cloudbolt_vulnerabilities.vuln_hooks",
    "xui.io_cloudbolt_vulnerabilities.vuln_jobs",
    "xui.io_cloudbolt_vulnerabilities.vuln_fleet",
)


def run(job, *args, **kwargs):
    # This runs inside every provisioning job once the setting is on: it must never fail the build.
    # A cancel is a BaseException and is not caught here.
    try:
        for name in MODULES:
            importlib.reload(importlib.import_module(name))
        fleet = importlib.import_module("xui.io_cloudbolt_vulnerabilities.vuln_fleet")
        return fleet.run_provision(job, **kwargs)
    except Exception as exc:
        return "SUCCESS", "The vulnerability scan was not started: {}".format(type(exc).__name__), "", {}
