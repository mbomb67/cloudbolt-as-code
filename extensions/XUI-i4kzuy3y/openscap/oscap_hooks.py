"""
The plugin the OpenSCAP job runs.

A CloudBoltHook whose module file is this extension's oscap_job.py, by
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


def scan_hook():
    """The plugin, created on first use and kept pointing at oscap_job.py."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCAN_FILE)
    hook = CloudBoltHook.objects.filter(name=SCAN_NAME).first()
    if hook is None:
        hook = CloudBoltHook(name=SCAN_NAME, description=SCAN_DESCRIPTION, shared=False)
        hook.module_file = path
        try:
            hook.save()
        except IntegrityError:
            # Another request created it first.
            hook = CloudBoltHook.objects.filter(name=SCAN_NAME).first()
            if hook is None:
                raise
    elif str(hook.module_file) != path:
        hook.module_file = path
        hook.save()
    return hook
