"""
The plugin the update job runs.

A CloudBoltHook whose module file is this extension's update_job.py, by
absolute path (the way CloudBolt's own built-in actions point at their
files). It is created the first time an update starts and re-pointed if the
extension ever moves, so nothing else has to be synced.
"""
import os

from cbhooks.models import CloudBoltHook

UPDATE_NAME = "Credentials Manager: Update Credentials"
UPDATE_DESCRIPTION = (
    "Writes new credentials onto the servers and images a Credentials Manager update chose. Created and "
    "run by the Credentials Manager UI extension; its code is the extension's update_job.py, so do not "
    "edit or delete it here."
)
UPDATE_FILE = "update_job.py"


def update_hook():
    """The plugin, created on first use and kept pointing at update_job.py."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), UPDATE_FILE)
    hook = CloudBoltHook.objects.filter(name=UPDATE_NAME).first()
    if hook is None:
        hook = CloudBoltHook(name=UPDATE_NAME, description=UPDATE_DESCRIPTION, shared=False)
        hook.module_file = path
        hook.save()
    elif str(hook.module_file) != path:
        hook.module_file = path
        hook.save()
    return hook


def existing_hook():
    """The plugin when it exists, without creating it."""
    return CloudBoltHook.objects.filter(name=UPDATE_NAME).first()
