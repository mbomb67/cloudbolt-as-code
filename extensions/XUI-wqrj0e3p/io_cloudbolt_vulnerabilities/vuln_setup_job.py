"""
Vulnerability Scanner Setup

The Vulnerabilities UI extension registers this file as a CloudBolt plugin the
first time an administrator sets up or updates the scanner, then runs it as a
job. The work itself lives in the extension (vuln_jobs.py). This file reloads
the extension's modules, so a job picks up an extension update without a job
engine restart, and hands over. Syncing the extension replaces this file; do
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
)


def run(job, *args, **kwargs):
    for name in MODULES:
        importlib.reload(importlib.import_module(name))
    jobs = importlib.import_module("xui.io_cloudbolt_vulnerabilities.vuln_jobs")
    return jobs.run_setup(job, **kwargs)
