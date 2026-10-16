"""
Content Package Manager: Export

The Content Package Manager UI extension registers this file as a CloudBolt
plugin the first time someone exports a package, then runs it as a job. The
export itself lives in the extension (jobs.py and services.py). This file
reloads those modules, so a job picks up an extension update without a job
engine restart, and hands over. Syncing the extension replaces this file; do
not edit it on the appliance.
"""
import importlib

# Reloaded in dependency order: each module imports the ones before it.
MODULES = (
    "xui.content_packages.store",
    "xui.content_packages.parameters",
    "xui.content_packages.hooks",
    "xui.content_packages.services",
    "xui.content_packages.jobs",
)


def run(job, *args, **kwargs):
    for name in MODULES:
        importlib.reload(importlib.import_module(name))
    jobs = importlib.import_module("xui.content_packages.jobs")
    return jobs.run_export(job, **kwargs)
