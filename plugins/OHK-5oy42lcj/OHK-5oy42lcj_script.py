"""
Display condition for Set StormForge Cluster Defaults (RSA-d3y9kiq0).

Shows the action only on a cluster where the StormForge agent install
succeeded: the install writes the agent version on the cluster resource once
Helm has finished. Any problem hides the action rather than breaking the
resource page.
"""

from shared_modules import stormforge


def should_display(resource, request=None):
    try:
        values = resource.get_cf_values_as_dict()
        return bool(values.get(stormforge.FIELD_CLUSTER) and values.get(stormforge.FIELD_VERSION))
    except Exception:  # noqa: BLE001
        return False
