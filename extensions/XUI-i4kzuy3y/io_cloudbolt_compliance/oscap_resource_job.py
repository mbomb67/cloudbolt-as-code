"""
Compliance Resource Scan

The Compliance UI extension registers this file as a CloudBolt plugin the first
time someone scans the servers of a resource, then runs it as a job. The work
itself lives in the extension (oscap_resource.py): the job starts one Compliance
Scan job per server, a few at a time, and records the run. This file reloads the
extension's modules, so a job picks up an extension update without a job engine
restart, and hands over. Syncing the extension replaces this file; do not edit
it on the appliance.
"""
import importlib

# Reloaded in dependency order: each module imports the ones before it.
MODULES = (
    "xui.io_cloudbolt_compliance.oscap_store",
    "xui.io_cloudbolt_compliance.oscap_repo",
    "xui.io_cloudbolt_compliance.oscap_hooks",
    "xui.io_cloudbolt_compliance.oscap_jobs",
    "xui.io_cloudbolt_compliance.oscap_rollup",
    "xui.io_cloudbolt_compliance.oscap_resource",
)


def run(job, *args, **kwargs):
    for name in MODULES:
        importlib.reload(importlib.import_module(name))
    resource = importlib.import_module("xui.io_cloudbolt_compliance.oscap_resource")
    return resource.run_resource_scan(job, **kwargs)
