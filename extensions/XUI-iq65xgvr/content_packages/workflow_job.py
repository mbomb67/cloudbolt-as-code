"""
Content Package Manager: Sync Workflow

The Content Package Manager UI extension registers this file as a CloudBolt
plugin the first time someone syncs a package that lists a Runbooks workflow,
then queues it as a job in the package's sync chain. The import itself lives
in the extension (jobs.py and workflows.py) and the Runbooks content's
runbook_workflows shared module. This file reloads the extension's modules,
so a job picks up an extension update without a job engine restart, and
hands over. Syncing the extension replaces this file; do not edit it on the
appliance.
"""
import importlib

# Reloaded in dependency order: each module imports the ones before it.
MODULES = (
    "xui.content_packages.store",
    "xui.content_packages.parameters",
    "xui.content_packages.workflows",
    "xui.content_packages.hooks",
    "xui.content_packages.services",
    "xui.content_packages.jobs",
)


def run(job, *args, **kwargs):
    for name in MODULES:
        importlib.reload(importlib.import_module(name))
    jobs = importlib.import_module("xui.content_packages.jobs")
    return jobs.run_workflow_sync(job, **kwargs)
