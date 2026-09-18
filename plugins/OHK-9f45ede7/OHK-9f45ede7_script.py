"""
Generic Bicep day-2 Update plugin.

Re-presents the deployment's current editable parameters (pre-filled from the
bicep_var_* mirrors via the option generator), re-fetches and recompiles the
template at its stored ref, assembles the edited parameter set, previews with
what-if behind the approval gate, and applies the change as a stack update.

Fails fast if a stack operation is already in flight (R14) so concurrent day-2
actions don't collide. Pinned parameters are never editable here. @secure()
parameters are handled by scaffold-generated typed (PWD/masked) inputs; this
generic JSON path is for non-secure parameters (secrets must not be pasted into
a plaintext, group-visible order input).

run(job, resource, **kwargs) — note the explicit `resource` positional, the
day-2 plugin signature. Returns (status, output_msg, error_msg).
"""
import json
import shutil
import tempfile

from common.methods import set_progress
from infrastructure.models import CustomField
from utilities.logger import ThreadLogger

from shared_modules.github import GitHubConnection, CONNECTION_INFO_NAME
from shared_modules.bicep_engine import (
    BicepArmClient,
    BicepEngineError,
    assemble_parameters,
    compile_template,
    extract_parameter_schema,
    fetch_template_checkout,
    resolve_stack_location,
    humanize_label,
    parse_params_payload,
    run_with_approval,
    SCOPE_RESOURCE_GROUP,
    SCOPE_SUBSCRIPTION,
    STACK_IN_FLIGHT,
    VAR_CF_PREFIX,
)

logger = ThreadLogger(__name__)


def _cf(resource, name):
    try:
        return resource.get_value_for_custom_field(name)
    except Exception:
        return None


def _current_mirrors(resource):
    """Current editable parameter values from the bicep_var_* mirror fields."""
    current = {}
    try:
        for k, v in resource.get_cf_values_as_dict().items():
            if k.startswith(VAR_CF_PREFIX):
                current[k[len(VAR_CF_PREFIX):]] = v
    except Exception:
        pass
    return current


def generate_options_for_parameters(field, **kwargs):
    """Pre-fill the dialog with the deployment's current editable values (from
    custom-field mirrors, not a live Azure read — keeps the dialog fast)."""
    resource = kwargs.get("resource")
    if not resource:
        return {"initial_value": "{}", "options": []}
    current = _current_mirrors(resource)
    return {"initial_value": json.dumps(current, indent=2), "options": []}


def _refresh_mirrors(resource, arm_params, schema):
    for pname, wrapped in arm_params.items():
        if schema.get(pname, {}).get("secure"):
            continue
        cf = f"{VAR_CF_PREFIX}{pname}"
        # show_on_servers default matches the build plugin's bicep_var_* fields;
        # creation-time default only (get_or_create won't override an existing CF).
        CustomField.objects.get_or_create(
            name=cf,
            defaults=dict(label=humanize_label(pname),
                          description="Bicep-managed field.", type="STR",
                          show_on_servers=True),
        )
        val = wrapped.get("value")
        resource.set_value_for_custom_field(
            cf, val if isinstance(val, str) else json.dumps(val))
    resource.save()


def run(job, resource, **kwargs):
    if resource is None:
        return "FAILURE", "", "No resource in job context."

    stack_name = _cf(resource, "bicep_stack_name")
    # Scope-aware addressing: rg is None for a subscription-scoped stack; a
    # resource with no stored scope predates scope support (RG-scoped).
    rg = _cf(resource, "bicep_resource_group") or None
    scope = _cf(resource, "bicep_target_scope") or (SCOPE_RESOURCE_GROUP if rg else None)
    rh_id = _cf(resource, "bicep_rh_id")
    repo = _cf(resource, "bicep_repo")
    template_path = _cf(resource, "bicep_template_path")
    ref = _cf(resource, "bicep_ref") or None
    if (not all([stack_name, rh_id, repo, template_path])
            or (scope != SCOPE_SUBSCRIPTION and not rg)):
        return ("FAILURE", "",
                "This resource is missing Bicep deployment metadata; cannot update.")

    edited_json = """{{ parameters }}"""

    from resourcehandlers.azure_arm.models import AzureARMHandler
    try:
        rh = AzureARMHandler.objects.get(id=int(rh_id))
    except (AzureARMHandler.DoesNotExist, ValueError, TypeError):
        return "FAILURE", "", f"Resource handler {rh_id} not found; cannot update."
    client = BicepArmClient(rh)

    # Concurrency fail-fast (R14): don't start while an op is in flight.
    state = client.provisioning_state(rg, stack_name)
    if state and state in STACK_IN_FLIGHT:
        return ("FAILURE", "",
                f"A change is already in progress on this deployment ({state}). "
                f"Wait for it to finish, then run Update again.")

    workdir = tempfile.mkdtemp(prefix="bicep-update-")
    try:
        set_progress(f"Fetching {repo}@{ref or 'default-branch'}...")
        with GitHubConnection.from_connection_name(CONNECTION_INFO_NAME) as gh:
            checkout = fetch_template_checkout(gh, repo, ref, template_path, workdir)
        arm_template = compile_template(checkout, template_path)
        schema = extract_parameter_schema(arm_template)

        pinned_raw = _cf(resource, "bicep_pinned_params")
        pinned = json.loads(pinned_raw) if pinned_raw else {}
        supplied = _current_mirrors(resource)
        # Accepts a JSON/Python-literal string and strips a Dynamic Panel's
        # single-item list[dict] to the dict (raises BicepEngineError on bad
        # input, caught below).
        supplied.update(parse_params_payload(edited_json))
        arm_params = assemble_parameters(schema, pinned, supplied)
        location = resolve_stack_location(arm_params) if scope == SCOPE_SUBSCRIPTION else None

        result = run_with_approval(
            job, client, rg, stack_name, arm_template, arm_params, schema,
            location=location,
            description=f"CloudBolt day-2 update {resource.global_id}",
        )
    except BicepEngineError as e:
        return "FAILURE", "", str(e)
    except Exception as e:  # noqa: BLE001 — surface a clean FAILURE, not a traceback
        logger.exception("Unexpected error during Bicep update")
        return "FAILURE", "", "Unexpected error during update; see the job log."
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    status = result.get("status")
    if status == "rejected":
        return "FAILURE", "Update rejected at the approval gate; nothing changed.", ""
    if status == "invalid":
        return ("FAILURE", "",
                f"The requested change cannot be applied: {result.get('error')} "
                f"Nothing was submitted to Azure.")
    if status != "succeeded":
        err = result.get("error") or f"provisioning state: {result.get('state')}"
        return "FAILURE", "", f"Update failed: {err}"

    _refresh_mirrors(resource, arm_params, schema)
    if not result.get("changes"):
        return ("SUCCESS",
                f"Re-applied deployment stack {stack_name}; what-if reported no "
                f"changes (the stack was converged to the submitted parameters).",
                "")
    return "SUCCESS", f"Updated deployment stack {stack_name}.", ""
