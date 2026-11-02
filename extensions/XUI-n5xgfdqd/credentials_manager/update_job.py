"""
Credentials Manager: Update Credentials

The Credentials Manager UI extension registers this file as a CloudBolt
plugin the first time someone starts an update, then runs it as a job. The
update itself lives in the extension (jobs.py and apply.py). This file
reloads those modules, so a job picks up an extension update without a job
engine restart, and hands over. Syncing the extension replaces this file; do
not edit it on the appliance.
"""
import importlib

# Reloaded in dependency order: each module imports the ones before it.
MODULES = (
    "xui.credentials_manager.targets",
    "xui.credentials_manager.apply",
    "xui.credentials_manager.hooks",
    "xui.credentials_manager.jobs",
)


def run(job, *args, **kwargs):
    for name in MODULES:
        importlib.reload(importlib.import_module(name))
    jobs = importlib.import_module("xui.credentials_manager.jobs")
    return jobs.run_update(job, **kwargs)
