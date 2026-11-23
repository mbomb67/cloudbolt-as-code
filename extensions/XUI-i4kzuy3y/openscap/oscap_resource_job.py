"""
OpenSCAP Resource Scan

The OpenSCAP UI extension registers this file as a CloudBolt plugin the first
time someone scans the servers of a resource, then runs it as a job. The work
itself lives in the extension (oscap_resource.py): the job starts one OpenSCAP
Scan job per server, a few at a time, and records the run. This file reloads the
extension's modules, so a job picks up an extension update without a job engine
restart, and hands over. Syncing the extension replaces this file; do not edit
it on the appliance.
"""
import importlib

# Reloaded in dependency order: each module imports the ones before it.
MODULES = (
    "xui.openscap.oscap_store",
    "xui.openscap.oscap_repo",
    "xui.openscap.oscap_hooks",
    "xui.openscap.oscap_jobs",
    "xui.openscap.oscap_rollup",
    "xui.openscap.oscap_resource",
)


def run(job, *args, **kwargs):
    for name in MODULES:
        importlib.reload(importlib.import_module(name))
    resource = importlib.import_module("xui.openscap.oscap_resource")
    return resource.run_resource_scan(job, **kwargs)
