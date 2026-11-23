"""
The plugins the Vulnerabilities jobs run.

Each is a CloudBoltHook whose module file is one of this extension's files, by
absolute path (the way CloudBolt's own built-in actions point at their
files). It is created the first time a scan or setup starts and re-pointed if
the extension ever moves, so nothing else has to be synced.
"""
import os

from django.db import IntegrityError

from cbhooks.models import CloudBoltHook

SCAN_NAME = "Vulnerability Scan"
SCAN_DESCRIPTION = (
    "Collects the software list of one server and matches it against the vulnerability database for the "
    "Vulnerabilities UI extension, and records the result. Created and run by the extension; its code is the "
    "extension's vuln_job.py, so do not edit or delete it here."
)
SCAN_FILE = "vuln_job.py"

SETUP_NAME = "Vulnerability Scanner Setup"
SETUP_DESCRIPTION = (
    "Installs or updates the scanner and the vulnerability database for the Vulnerabilities UI extension. "
    "Created and run by the extension; its code is the extension's vuln_setup_job.py, so do not edit or "
    "delete it here."
)
SETUP_FILE = "vuln_setup_job.py"


def _hook(name, description, filename):
    """The plugin called name, created on first use and kept pointing at filename."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    hook = CloudBoltHook.objects.filter(name=name).first()
    if hook is None:
        hook = CloudBoltHook(name=name, description=description, shared=False)
        hook.module_file = path
        try:
            hook.save()
        except IntegrityError:
            # Another request created it first.
            hook = CloudBoltHook.objects.filter(name=name).first()
            if hook is None:
                raise
    elif str(hook.module_file) != path:
        hook.module_file = path
        hook.save()
    return hook


def scan_hook():
    """The per-server plugin, created on first use and kept pointing at vuln_job.py."""
    return _hook(SCAN_NAME, SCAN_DESCRIPTION, SCAN_FILE)


def setup_hook():
    """The scanner setup plugin, created on first use and kept pointing at vuln_setup_job.py."""
    return _hook(SETUP_NAME, SETUP_DESCRIPTION, SETUP_FILE)
