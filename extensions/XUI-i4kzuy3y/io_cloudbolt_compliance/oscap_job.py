"""
Compliance Scan

The Compliance UI extension registers this file as a CloudBolt plugin the first
time someone starts a scan or check, then runs it as a job. The work itself
lives in the extension (oscap_jobs.py). This file reloads the extension's
modules, so a job picks up an extension update without a job engine restart,
and hands over. Syncing the extension replaces this file; do not edit it on
the appliance.
"""
import importlib

# Reloaded in dependency order: each module imports the ones before it.
MODULES = (
    "xui.io_cloudbolt_compliance.oscap_store",
    "xui.io_cloudbolt_compliance.oscap_repo",
    "xui.io_cloudbolt_compliance.oscap_hooks",
    "xui.io_cloudbolt_compliance.oscap_windows",
    "xui.io_cloudbolt_compliance.oscap_jobs",
)


def run(job, *args, **kwargs):
    for name in MODULES:
        importlib.reload(importlib.import_module(name))
    jobs = importlib.import_module("xui.io_cloudbolt_compliance.oscap_jobs")
    return jobs.run_scan(job, **kwargs)
