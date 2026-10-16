"""
The plugins this extension runs its jobs with.

Each is a CloudBoltHook whose module file is one of this extension's files, by
absolute path (the way CloudBolt's own built-in actions point at their files).
It is created the first time it is needed and re-pointed if the extension ever
moves, so nothing else has to be synced.
"""
import os

from cbhooks.models import CloudBoltHook

EXPORT_NAME = "Content Package Manager: Export"
EXPORT_DESCRIPTION = (
    "Pushes a content package to a repository. Created and run by the Content Package Manager UI "
    "extension; its code is the extension's export_job.py, so do not edit or delete it here."
)
EXPORT_FILE = "export_job.py"

PARAMETER_NAME = "Content Package Manager: Sync Parameter"
PARAMETER_DESCRIPTION = (
    "Creates or updates a parameter from a content package's repository branch. Created and run by the "
    "Content Package Manager UI extension; its code is the extension's parameter_job.py, so do not edit "
    "or delete it here."
)
PARAMETER_FILE = "parameter_job.py"


def hook_file(filename):
    """Absolute path of one of this extension's files."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def plugin(name, description, filename):
    """The plugin named name, created on first use and kept pointing at this
    extension's filename."""
    path = hook_file(filename)
    hook = CloudBoltHook.objects.filter(name=name).first()
    if hook is None:
        hook = CloudBoltHook(name=name, description=description, shared=False)
        hook.module_file = path
        hook.save()
    elif str(hook.module_file) != path:
        hook.module_file = path
        hook.save()
    return hook


def export_hook():
    return plugin(EXPORT_NAME, EXPORT_DESCRIPTION, EXPORT_FILE)


def parameter_hook():
    return plugin(PARAMETER_NAME, PARAMETER_DESCRIPTION, PARAMETER_FILE)
