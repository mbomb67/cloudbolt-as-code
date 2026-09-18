"""
Generic Bicep day-2 Drift Check plugin.

Runs a read-only what-if (plus the stack managed-resource diff) using the
deployment's current stored parameters and writes the change set to job output.
Changes nothing — no pause, no apply. @secure() values are excluded from the
output by the engine's redaction.

run(job, resource, **kwargs). Returns (status, output_msg, error_msg).
"""
import json
import shutil
import tempfile

from common.methods import set_progress
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
    run_with_approval,
    SCOPE_RESOURCE_GROUP,
    SCOPE_SUBSCRIPTION,
    VAR_CF_PREFIX,
)

logger = ThreadLogger(__name__)


def _cf(resource, name):
    try:
        return resource.get_value_for_custom_field(name)
    except Exception:
        return None


def _current_mirrors(resource):
    current = {}
    try:
        for k, v in resource.get_cf_values_as_dict().items():
            if k.startswith(VAR_CF_PREFIX):
                current[k[len(VAR_CF_PREFIX):]] = v
    except Exception:
        pass
    return current


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
                "This resource is missing Bicep deployment metadata; cannot check drift.")

    from resourcehandlers.azure_arm.models import AzureARMHandler
    try:
        rh = AzureARMHandler.objects.get(id=int(rh_id))
    except (AzureARMHandler.DoesNotExist, ValueError, TypeError):
        return "FAILURE", "", f"Resource handler {rh_id} not found; cannot check drift."
    client = BicepArmClient(rh)

    workdir = tempfile.mkdtemp(prefix="bicep-drift-")
    try:
        set_progress(f"Fetching {repo}@{ref or 'default-branch'} for drift check...")
        with GitHubConnection.from_connection_name(CONNECTION_INFO_NAME) as gh:
            checkout = fetch_template_checkout(gh, repo, ref, template_path, workdir)
        arm_template = compile_template(checkout, template_path)
        schema = extract_parameter_schema(arm_template)

        pinned_raw = _cf(resource, "bicep_pinned_params")
        pinned = json.loads(pinned_raw) if pinned_raw else {}
        supplied = _current_mirrors(resource)
        arm_params = assemble_parameters(schema, pinned, supplied)
        location = resolve_stack_location(arm_params) if scope == SCOPE_SUBSCRIPTION else None

        result = run_with_approval(
            job, client, rg, stack_name, arm_template, arm_params, schema,
            location=location,
            is_drift=True,
        )
    except BicepEngineError as e:
        return "FAILURE", "", str(e)
    except Exception as e:  # noqa: BLE001 — surface a clean FAILURE, not a traceback
        logger.exception("Unexpected error during drift check")
        return "FAILURE", "", "Unexpected error during drift check; see the job log."
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if result.get("error"):
        return ("WARNING",
                f"Drift check could not complete — what-if reported: "
                f"{result.get('error')}", "")
    if not result.get("whatif_available", True):
        return ("WARNING",
                "Drift status UNKNOWN — the what-if preview was unavailable, so "
                "the deployment could not be compared against the template. "
                "Check Azure connectivity and retry.", "")
    changes = result.get("changes") or []
    if not changes:
        return ("SUCCESS",
                "No drift detected: the deployment matches the template with its "
                "current parameters.", "")
    return ("SUCCESS",
            f"Drift preview complete — {len(changes)} change(s) reported in the "
            f"job output above. Nothing was applied.", "")
