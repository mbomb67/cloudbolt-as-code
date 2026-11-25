"""
Display condition for Apply StormForge Recommendation (RSA-d2zufawc) and Set
StormForge Optimization Settings (RSA-v35tl317).

Shows the actions only on a namespace whose cluster runs the StormForge agent,
on a CloudBolt that has the StormForge connection info. Any problem hides the
actions rather than breaking the resource page.
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
