"""
Writes new credentials onto one server or image and records the change in
CloudBolt's history.

The values go where CloudBolt's own credentials dialogs put them (the
username, password and key_name parameters; common/forms.py
EditCBObjCredentialsForm), and setting a server's username or password also
removes the legacy parameters the native dialog removes, so they cannot
shadow the new value. Nothing on the server or in the image itself changes.

History: a server gets a Server Change Event naming the user and the job;
CloudBolt drops change events on a server that has no creation or onboard
event (utilities/events.py add_server_event), so such a server gets a
notification event instead. Images have no history of their own, so the
event goes on the image's resource handler.
"""
from django.db import transaction

from utilities.events import add_server_event

from xui.credentials_manager.targets import (
    KEY_CF, LEGACY_PASSWORD_CFS, LEGACY_USERNAME_CFS, NEW_VM_PASSWORD_CF, PASSWORD_CF, USERNAME_CF,
)

SOURCE = "Credentials Manager"


def _remove(obj, cf_names):
    cfvs = obj.get_cfv_manager()
    old = list(cfvs.filter(field__name__in=cf_names))
    if old:
        cfvs.remove(*old)


def _current_username(obj, legacy=()):
    for name in (USERNAME_CF,) + tuple(legacy):
        value = obj.get_value_for_custom_field(name)
        if value:
            return value
    return ""


def _set_credentials(obj, fields, update, legacy_usernames=(), legacy_passwords=()):
    """Write the fields named in fields; return the phrases describing them."""
    said = []
    if "username" in fields:
        before = _current_username(obj, legacy_usernames)
        obj.set_value_for_custom_field(USERNAME_CF, update["username"])
        _remove(obj, legacy_usernames)
        said.append("username {} → {}".format(before or "(none)", update["username"]))
    if "password" in fields:
        obj.set_value_for_custom_field(PASSWORD_CF, update["password"])
        _remove(obj, legacy_passwords)
        said.append("password changed")
    if "ssh_key" in fields:
        # Same as the native dialog: detach any key, attach the chosen key's
        # reference value.
        _remove(obj, (KEY_CF,))
        obj.get_cfv_manager().add(update["key_cfv"])
        said.append("SSH key set to {}".format(update["key_label"]))
    if "new_vm_password" in fields:
        obj.set_value_for_custom_field(NEW_VM_PASSWORD_CF, update["new_vm_password"])
        said.append("new VM password changed")
    return said


def apply_to_server(server, fields, update, profile, job):
    """Update one server and add its history event. Returns the event text."""
    with transaction.atomic():
        said = _set_credentials(server, fields, update, LEGACY_USERNAME_CFS, LEGACY_PASSWORD_CFS)
        message = "Credentials updated from {}: {}.".format(SOURCE, ", ".join(said))
        onboarded = server.serverhistory_set.filter(event_type__in=["CREATION", "ONBOARD"]).exists()
        add_server_event("MODIFICATION" if onboarded else "INFO", server, message, profile=profile, job=job)
    if job is not None:
        server.jobs.add(job)
    return message


def image_name(osba):
    return osba.template_name or (osba.os_build.name if osba.os_build else "Image {}".format(osba.id))


def apply_to_image(osba, fields, update, profile, job):
    """Update one image and add the event to its resource handler's history."""
    with transaction.atomic():
        said = _set_credentials(osba, fields, update)
        build = " (OS build {})".format(osba.os_build.name) if osba.os_build else ""
        message = "Credentials of image {}{} updated from {}: {}.".format(image_name(osba), build, SOURCE, ", ".join(said))
        osba.resourcehandler.add_event("MODIFICATION", message, profile=profile, job=job)
    return message
