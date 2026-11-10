"""
Display condition for Apply StormForge Recommendation (RSA-d2zufawc).

Shows the action only on a namespace whose cluster runs the StormForge agent,
on a CloudBolt that has the StormForge connection info. Any problem hides the
action rather than breaking the resource page.
"""

from utilities.models import ConnectionInfo

from shared_modules import stormforge


def should_display(resource, request=None):
    try:
        if stormforge.namespace_scope(resource) is None:
            return False
        return ConnectionInfo.objects.filter(name=stormforge.CONNECTION_INFO_NAME).exists()
    except Exception:  # noqa: BLE001
        return False
