"""
Vulnerability Install Updates

The Vulnerabilities UI extension registers this file as a CloudBolt plugin and shows it as the Install Updates
server action when an administrator turns on the patching setting. It installs the updates a Linux server's package
manager or Windows Update offers (Linux, and Windows Server 2022 and 2025), restarts the server when that is needed and allowed, and scans it again. The work itself lives in the
extension (vuln_patch.py). This file reloads the extension's modules, so a job picks up an extension update without a
job engine restart, and hands over. Syncing the extension replaces this file; do not edit it on the appliance.

The line below declares the action's one input, "Restart if needed". CloudBolt reads it when the plugin is saved and
creates the input from it; the value reaches run() as the keyword restart_if_needed, so the line itself is never read.
"""
import importlib

# Triple quotes, so a line break in the value cannot end the string.
RESTART_TOKEN = """{{ restart_if_needed }}"""

# Reloaded in dependency order: each module imports the ones before it.
MODULES = (
    "xui.io_cloudbolt_vulnerabilities.vuln_store",
    "xui.io_cloudbolt_vulnerabilities.vuln_match",
    "xui.io_cloudbolt_vulnerabilities.vuln_db",
    "xui.io_cloudbolt_vulnerabilities.vuln_msrc",
    "xui.io_cloudbolt_vulnerabilities.vuln_tools",
    "xui.io_cloudbolt_vulnerabilities.vuln_repo",
    "xui.io_cloudbolt_vulnerabilities.vuln_hooks",
    "xui.io_cloudbolt_vulnerabilities.vuln_jobs",
    "xui.io_cloudbolt_vulnerabilities.vuln_fleet",
    "xui.io_cloudbolt_vulnerabilities.vuln_wintask",
    "xui.io_cloudbolt_vulnerabilities.vuln_patch",
)


def run(job, *args, **kwargs):
    for name in MODULES:
        importlib.reload(importlib.import_module(name))
    patch = importlib.import_module("xui.io_cloudbolt_vulnerabilities.vuln_patch")
    return patch.run_patch(job, **kwargs)
