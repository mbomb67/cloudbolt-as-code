"""
The plugins the OpenSCAP jobs run.

Each is a CloudBoltHook whose module file is one of this extension's files, by
absolute path (the way CloudBolt's own built-in actions point at their
files). It is created the first time a scan or check starts and re-pointed if
the extension ever moves, so nothing else has to be synced.
"""
import os

from django.db import IntegrityError

from cbhooks.models import CloudBoltHook

SCAN_NAME = "OpenSCAP Scan"
SCAN_DESCRIPTION = (
    "Runs an OpenSCAP scan or readiness check on one server for the OpenSCAP UI extension and records "
    "the result. Created and run by the extension; its code is the extension's oscap_job.py, so do not "
    "edit or delete it here."
)
SCAN_FILE = "oscap_job.py"

RESOURCE_NAME = "OpenSCAP Resource Scan"
RESOURCE_DESCRIPTION = (
    "Runs an OpenSCAP scan or readiness check on the servers of one resource for the OpenSCAP UI "
    "extension: it starts one OpenSCAP Scan job per server, a few at a time, and records the run. "
    "Created and run by the extension; its code is the extension's oscap_resource_job.py, so do not "
    "edit or delete it here."
)
RESOURCE_FILE = "oscap_resource_job.py"


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
    """The per-server plugin, created on first use and kept pointing at oscap_job.py."""
    return _hook(SCAN_NAME, SCAN_DESCRIPTION, SCAN_FILE)


def resource_scan_hook():
    """The per-resource plugin, created on first use and kept pointing at oscap_resource_job.py."""
    return _hook(RESOURCE_NAME, RESOURCE_DESCRIPTION, RESOURCE_FILE)
