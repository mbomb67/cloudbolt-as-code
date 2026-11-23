"""
OpenSCAP Scan

The OpenSCAP UI extension registers this file as a CloudBolt plugin the first
time someone starts a scan or check, then runs it as a job. The work itself
lives in the extension (oscap_jobs.py). This file reloads the extension's
modules, so a job picks up an extension update without a job engine restart,
and hands over. Syncing the extension replaces this file; do not edit it on
the appliance.
"""
import importlib

# Reloaded in dependency order: each module imports the ones before it.
MODULES = (
    "xui.openscap.oscap_store",
    "xui.openscap.oscap_repo",
    "xui.openscap.oscap_hooks",
    "xui.openscap.oscap_windows",
    "xui.openscap.oscap_jobs",
)


def run(job, *args, **kwargs):
    for name in MODULES:
        importlib.reload(importlib.import_module(name))
    jobs = importlib.import_module("xui.openscap.oscap_jobs")
    return jobs.run_scan(job, **kwargs)
