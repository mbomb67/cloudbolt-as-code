"""
Run Blueprint Discovery (MCP Tool Action plugin).

Launches the same Sync Resources job that the Sync Resources button on a
blueprint's Discovery tab launches (servicecatalog.views
.sync_resources_on_discovery_tab), scoped to one blueprint, so an agent can
exercise a discovery plugin on demand and poll the job.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically.
"""
import html

from cbhooks.models import CloudBoltHook
from servicecatalog.models import ServiceBlueprint

SYNC_RESOURCES_HOOK_NAME = "Sync Resources"


def _text(value):
    return html.unescape(value or "").strip()


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


def _read_inputs():
    raw = {"discovery_blueprint": """{{ discovery_blueprint }}"""}
    return {"blueprint": _text(raw["discovery_blueprint"])}


def _find_blueprint(ref):
    blueprint = ServiceBlueprint.objects.filter(global_id=ref).first()
    if blueprint is not None:
        return blueprint, None
    matches = ServiceBlueprint.objects.filter(name__iexact=ref).exclude(status="HISTORICAL")
    if matches.count() == 1:
        return matches.first(), None
    if matches.count() > 1:
        return None, "Several active blueprints are named '{}': {}. Give the global id.".format(
            ref, ", ".join(bp.global_id for bp in matches)
        )
    return None, "No active blueprint matches '{}'. Give a BP-... global id or the exact name.".format(ref)


def run(job, *args, **kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    if profile is None:
        return _failure(
            "No calling user profile was supplied. Run this tool through the MCP "
            "server or the API as an authenticated user."
        )

    inputs = _read_inputs()
    if not inputs["blueprint"]:
        return _failure("discovery_blueprint is required: a BP-... global id or the exact name.")
    blueprint, error = _find_blueprint(inputs["blueprint"])
    if error:
        return _failure(error)
    if not (profile.is_cbadmin or blueprint.is_manager(profile)):
        return _failure("You must manage blueprint '{}' to run its discovery.".format(blueprint.name))
    if blueprint.status == "HISTORICAL":
        return _failure("Blueprint '{}' is historical; discovery does not run for it.".format(blueprint.name))
    if not blueprint.discovery_plugin:
        return _failure("Blueprint '{}' has no discovery plugin.".format(blueprint.name))

    hook = CloudBoltHook.objects.filter(name=SYNC_RESOURCES_HOOK_NAME).first()
    action = hook.recurringactionjob_set.first() if hook is not None else None
    if action is None:
        return _failure(
            "The '{}' plug-in or its recurring job action is missing on this "
            "appliance, so discovery cannot be launched.".format(SYNC_RESOURCES_HOOK_NAME)
        )

    jobs = action.run_hook_as_job(sync_bp_id=blueprint.id, owner=profile)
    sync_job = jobs[0]
    outputs = {
        "jobId": sync_job.global_id,
        "blueprintId": blueprint.global_id,
        "blueprintName": blueprint.name,
        "discoveryPlugin": str(blueprint.discovery_plugin),
        "autoHistoricalResources": bool(getattr(blueprint, "auto_historical_resources", False)),
        "next": (
            "Poll fetch_job_log with jobId; the progress log names each resource "
            "created, updated, or marked historical."
        ),
    }
    message = "Created discovery job {} for blueprint '{}'.".format(
        sync_job.global_id, blueprint.name
    )
    return {
        "status": "SUCCESS",
        "output_message": message,
        "error_message": "",
        "outputs": outputs,
    }
