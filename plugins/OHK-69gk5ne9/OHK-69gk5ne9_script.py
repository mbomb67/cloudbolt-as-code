"""
CloudBolt plug-in: Run Workflow, the plug-in of the Run Workflow action
(server_actions/SVA-j7rbsh39) and a blueprint build item.

Runs a workflow, a named, ordered list of CloudBolt actions built on
the Runbooks admin page (extensions/XUI-jbm5ac9e), against the
selected VM. The workflows, their filters, the prompts and the step
runner live in the runbook_workflows shared module
(shared_modules/SHM-nznzhpp0).

As a blueprint build item (after the VM item, for example
blueprints/BP-vy9ytey6) it runs on the VMs the order built: the
resource's VMs when the blueprint creates a resource, else the VMs its VM
items built (CloudBolt then passes a build item neither a resource nor
VMs). An admin pins the workflow by giving the item's Workflow input a
default (the workflow's name or SWF- id), which hides it on the order form;
without a default the user picks one. A prompt the order does not answer in
Workflow inputs takes the value of a parameter with the same name on the
resource, else on the VM, so order-form fields can feed the workflow.
The run's outputs become the item's outputs for later build items.

Inputs (declared in OHK-69gk5ne9_metadata.json):
  workflow         (STR, required)  the workflow's SWF- id (its exact name also
                                    works). generate_options_for_workflow lists
                                    the enabled workflows whose filters match
                                    the VM, or on a blueprint order form those
                                    whose OS filters fit the blueprint's VM
                                    items.
  workflow_inputs  (TXT, optional)  the values of the step inputs the workflow
                                    asks for. The custom form
                                    (forms/FRM-ecw50rd0) sends them as a
                                    Dynamic Panel, which arrives as a list
                                    holding one dict of '<step key>__<input>'
                                    names; the API and MCP send a JSON object,
                                    {"<step key>": {"<input>": value}} or the
                                    flat names. Plain text in the job's
                                    parameters and log, so never a password.

A custom-form submit is not validated by CloudBolt, so this plug-in checks the
workflow against the VM, every step's action against the VM, and
every value against its input before the first step runs. With several
servers selected, each VM gets its own child job, which runs this
plug-in for that one VM, so each VM's step outputs stay apart.
"""
import json

from common.methods import set_progress
from jobs.models import Job
from utilities.logger import ThreadLogger

from shared_modules import runbook_workflows as swf

logger = ThreadLogger(__name__)

# CloudBolt rebuilds a plug-in's inputs from the template variables in its
# source on every save and deletes the others. The line below names both
# inputs inside a block that never renders, so no submitted value is ever
# written into this code; run() reads the values from its kwargs.
# {% if False %}"{{ workflow }}" """{{ workflow_inputs }}"""{% endif %}


def _targets(server=None, servers=None):
    targets = list(servers or [])
    if not targets and server is not None:
        targets = [server]
    return targets


def _lineage(os_family):
    """The OS family and its parents (Amazon Linux -> Linux)."""
    families = []
    while os_family is not None and os_family not in families:
        families.append(os_family)
        os_family = os_family.parent
    return families


def _blueprint_os_families(blueprint):
    """
    The OS families the blueprint's VM items can build: each item's pinned OS
    build's family, else its allowed OS families (CloudBolt also offers builds
    of a family under one of them). None when an item allows any OS family or
    the blueprint has no VM item.
    """
    families = set()
    for item in blueprint.pssis():
        if item.os_build_id and item.os_build.os_family_id:
            allowed = {item.os_build.os_family}
        else:
            allowed = set(item.allowable_os_families.all())
        if not allowed:
            return None
        families |= allowed
    return families or None


def _fits_os_families(workflow, families):
    """Whether a VM of one of families, or of a family under one, can pass the workflow's OS filters."""
    resolved = swf.resolve_filters(workflow.get("filters"))
    wanted = set(resolved["os_families"]) | {build.os_family for build in resolved["os_builds"] if build.os_family_id}
    if not wanted:
        return True
    return any(want in _lineage(family) or family in _lineage(want) for want in wanted for family in families)


def generate_options_for_workflow(field=None, server=None, servers=None, blueprint=None, **kwargs):
    """
    The enabled workflows offered on the VM, or on every VM of a bulk run.
    On a blueprint order form, where no VM exists yet, the enabled workflows
    whose OS family and OS build filters fit what the blueprint's VM items
    can build, so a Linux blueprint offers no Windows workflow.
    """
    targets = _targets(server, servers)
    if targets or blueprint is None:
        return swf.workflow_options(targets)
    workflows = swf.workflows_for_servers([])
    families = _blueprint_os_families(blueprint)
    if families:
        workflows = [wf for wf in workflows if _fits_os_families(wf, families)]
    return [(wf["id"], wf["name"]) for wf in workflows]


def generate_options_for_workflow_inputs(field=None, control_value=None, **kwargs):
    """Pre-fills Workflow inputs with the chosen workflow's prompts, for the native order form."""
    if not control_value:
        return {"initial_value": ""}
    try:
        return {"initial_value": swf.prompt_skeleton(swf.resolve_workflow(control_value))}
    except swf.WorkflowError:
        return {"initial_value": ""}


def _runner_hook(job):
    """This plug-in, from the job that is running it."""
    params = job.job_parameters.cast()
    hook = getattr(params, "hook", None)
    if hook is None:
        action = getattr(params, "action_type", None)
        hook = getattr(action, "hook", None)
    return hook.cast() if hook is not None else None


def _run_per_server(job, workflow, targets, values):
    """
    One child job per VM, all started at once; returns the combined
    result, with each VM's step outputs under its host name. values maps
    each VM's id to its prompt values.
    """
    hook = _runner_hook(job)
    if hook is None:
        return "FAILURE", "", "Could not find the Run Workflow plug-in from this job."
    children = []
    for target in targets:
        jobs = hook.run_as_job(
            owner=job.owner,
            servers=[target],
            parent_job=job,
            context={"workflow": workflow["id"], "workflow_inputs": json.dumps(values[target.id])},
        )
        if not jobs:
            return "FAILURE", "", f"CloudBolt did not start the workflow on {target.hostname}."
        children.append((target, jobs[0]))
    set_progress(f"Workflow '{workflow['name']}' started on {len(children)} VMs, one job each.")
    Job.wait_for_jobs([child for _, child in children])
    lines, failures, warnings, outputs = [], [], [], {}
    for target, child in children:
        child = Job.objects.get(pk=child.pk)
        outputs[target.hostname] = child.outputs if isinstance(child.outputs, dict) else {}
        lines.append(f"{target.hostname}: {child.status} (job {child.id})")
        if child.status == "WARNING":
            warnings.append(target.hostname)
        elif child.status != "SUCCESS":
            failures.append(target.hostname)
    output = "\n".join(lines)
    if failures:
        status, error = "FAILURE", f"The workflow did not succeed on {', '.join(failures)}."
    elif warnings:
        status, error = "WARNING", f"The workflow ended with warnings on {', '.join(warnings)}."
    else:
        status, error = "SUCCESS", ""
    return {"status": status, "output_message": output, "error_message": error, "outputs": {"servers": outputs}}


def _resource_servers(resource):
    return list(resource.server_set.filter(status="ACTIVE").order_by("hostname"))


def _order_servers(job):
    """
    The active VMs a blueprint order built, for a blueprint that creates no
    resource. Each VM item runs as a job under the same deploy job as this
    build item, and CloudBolt links each VM to the job that built it.
    """
    parent = getattr(job, "parent_job", None) if job is not None else None
    if parent is None:
        return []
    from infrastructure.models import Server

    return list(
        Server.objects.filter(jobs__parent_job=parent, status="ACTIVE").distinct().order_by("hostname")
    )


def run(job=None, server=None, servers=None, resource=None, workflow=None, workflow_inputs=None, **kwargs):
    targets = _targets(server, servers)
    # No VMs passed in: a blueprint build item.
    from_blueprint = not targets
    if from_blueprint:
        targets = _resource_servers(resource) if resource is not None else _order_servers(job)
        if not targets:
            where = f"on {resource.name}" if resource is not None else "in this order"
            return (
                "FAILURE",
                "",
                f"Run Workflow found no active VMs {where}; put this build item after the "
                "blueprint's VM item.",
            )

    try:
        chosen = swf.resolve_workflow(workflow)
        if not chosen.get("enabled", True):
            raise swf.WorkflowError(f"Workflow '{chosen['name']}' is disabled.")
        steps = swf.load_steps(chosen)
        values = {}
        for target in targets:
            fallback = swf.order_values(resource, target) if from_blueprint else None
            values[target.id] = swf.parse_prompt_values(chosen, steps, workflow_inputs, fallback=fallback)
        problems = swf.preflight(chosen, steps, targets, values)
    except swf.WorkflowError as err:
        return "FAILURE", "", str(err)
    if problems:
        return "FAILURE", "", "The workflow cannot start:\n" + "\n".join(problems)

    set_progress(
        f"Workflow '{chosen['name']}': {len(steps)} step(s) on {len(targets)} VM(s): "
        + ", ".join(step.name for step in steps)
    )
    if len(targets) > 1:
        return _run_per_server(job, chosen, targets, values)

    status, output, errors, outputs = swf.run_workflow(chosen, targets[0], values[targets[0].id], job=job)
    return {"status": status, "output_message": output, "error_message": errors, "outputs": outputs}
