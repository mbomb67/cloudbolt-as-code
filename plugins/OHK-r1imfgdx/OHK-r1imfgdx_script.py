"""
Add Resource Group to Environment — optional second build step for the Bicep
resource-group blueprint.

Runs AFTER the generic Bicep build has created the resource group. When the
orderer ticks "Add Resource Group to Environment", the new resource group's
name is added as an option of CloudBolt's platform `resource_group_arm`
parameter on the Environment the order was provisioned into, so it shows up
immediately in that environment's Resource Group dropdown for VM orders — no
resource-handler sync needed. Unticked, the step is a no-op.

Reads from the resource (all written by the Bicep build plugin OHK-gqvi9kv4):
  bicep_env_id       : the Environment the order provisioned into
  bicep_var_rgName   : the created resource group's name (parameter mirror);
                       other candidate fields are tried, then resource.name

Mechanics: Environment.custom_field_options is a M2M to CustomFieldValue, and a
CustomFieldValue is shared per (field, value) across environments — so the
value is get_or_create'd and only the LINK to this environment is added. The
environments touched are recorded on the resource (bicep_rg_option_env_ids)
for the paired teardown plugin OHK-vm5p34w3, which removes the link and never
deletes the shared value.

Action inputs (OHK-r1imfgdx_metadata.json):
  add_to_environment (BOOL) : tick to add the option; unticked = skip

Outcomes: SUCCESS (added / already present / skipped by choice). WARNING — not
FAILURE — when the resource lacks the metadata this step needs: the resource
group itself exists, so an optional convenience step must not fail the build.

Returns (status, output_msg, error_msg).
"""
import json

from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from orders.models import CustomFieldValue
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# CloudBolt's platform parameter behind the Azure "Resource Group" dropdown;
# environments carry its options in custom_field_options (see the native
# generate_options_for_resource_group in plugins/OHK-7987st2p).
RG_PARAMETER_NAME = "resource_group_arm"

# Where the created resource group's name lives on the resource, in priority
# order (parameter mirrors first, then outputs), before falling back to
# resource.name. Keep in sync with the teardown plugin OHK-vm5p34w3.
RG_NAME_SOURCES = [
    "bicep_var_rgName",
    "bicep_var_resourceGroupName",
    "bicep_var_name",
    "bicep_out_resourceGroupName",
    "bicep_out_rgName",
]

# JSON list of Environment ids whose resource_group_arm options this resource
# added itself to; the teardown plugin removes exactly these (plus any other
# environment on the same subscription that offers the name).
TRACKING_CF = "bicep_rg_option_env_ids"


def _cf(resource, name):
    try:
        return resource.get_value_for_custom_field(name)
    except Exception:
        return None


def _truthy(raw):
    """BOOL inputs render as 'True'/'False' (or true/false from a custom form)."""
    return str(raw).strip().lower() in ("true", "1", "yes", "on", "checked")


def resolve_rg_name(resource):
    for name in RG_NAME_SOURCES:
        val = _cf(resource, name)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return (getattr(resource, "name", "") or "").strip()


def _ensure_tracking_cf():
    # Visibility flags are creation-time defaults only (get_or_create never
    # overrides an operator's later retuning).
    CustomField.objects.get_or_create(
        name=TRACKING_CF,
        defaults=dict(
            label="Bicep RG Option Environments",
            description=("JSON list of Environment ids whose resource_group_arm "
                         "options include this resource group (added by the "
                         "Azure Resource Group - Bicep blueprint)."),
            type="TXT", show_on_servers=False, show_as_attribute=False,
        ),
    )


def _record_env(resource, env_id):
    _ensure_tracking_cf()
    raw = _cf(resource, TRACKING_CF)
    try:
        ids = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        ids = []
    if not isinstance(ids, list):
        ids = []
    if env_id not in ids:
        ids.append(env_id)
    resource.set_value_for_custom_field(TRACKING_CF, json.dumps(ids))
    resource.save()


def run(job, **kwargs):
    resource = kwargs.get("resource")
    if resource is None:
        return "FAILURE", "", "No resource in job context."
    
    add_to_environment = "{{ add_to_environment }}"
    set_progress(f'add_to_environment: {add_to_environment}')
    
    # Quote every templated input (cardinal rule 3).
    if not _truthy(add_to_environment):
        return ("SUCCESS",
                "Resource group was not added to the environment's Resource Group "
                "options (option not selected).", "")

    rg_name = resolve_rg_name(resource)
    if not rg_name:
        return ("WARNING", "",
                "Could not determine the created resource group's name from the "
                "resource; nothing was added to the environment.")

    env_id = _cf(resource, "bicep_env_id")
    try:
        env = Environment.objects.get(id=int(env_id))
    except (Environment.DoesNotExist, TypeError, ValueError):
        return ("WARNING", "",
                f"The provisioning Environment could not be resolved (bicep_env_id="
                f"{env_id!r}); resource group '{rg_name}' was not added to any "
                f"environment.")

    cf = CustomField.objects.filter(name=RG_PARAMETER_NAME).first()
    if cf is None:
        return ("WARNING", "",
                f"Parameter '{RG_PARAMETER_NAME}' does not exist on this CloudBolt; "
                f"nothing was added.")

    set_progress(f"Adding resource group '{rg_name}' to the Resource Group options "
                 f"of environment '{env.name}'...")
    # STR parameter -> str_value column (the native RG hook reads rg.str_value).
    cfv, _ = CustomFieldValue.objects.get_or_create(field=cf, str_value=rg_name)
    already = env.custom_field_options.filter(id=cfv.id).exists()
    if not already:
        env.custom_field_options.add(cfv)
    _record_env(resource, env.id)

    note = ""
    if not env.custom_fields.filter(id=cf.id).exists():
        # The option is stored but the dropdown only renders parameters that
        # are enabled on the environment; tell the operator rather than
        # silently enabling a parameter they did not choose.
        note = (f" Note: '{RG_PARAMETER_NAME}' is not enabled as a parameter on "
                f"'{env.name}', so the option will not show until it is.")
    verb = "already offered" if already else "added as an option of"
    return ("SUCCESS",
            f"Resource group '{rg_name}' {verb} '{cf.label or cf.name}' on "
            f"environment '{env.name}'.{note}", "")