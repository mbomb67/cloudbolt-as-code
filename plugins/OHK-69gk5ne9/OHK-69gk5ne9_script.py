"""
CloudBolt server action plug-in: Run Workflow (server_actions/SVA-j7rbsh39).

Runs a server workflow, a named, ordered list of CloudBolt actions built on
the Server Workflows admin page (extensions/XUI-jbm5ac9e), against the
selected server. The workflows, their filters, the prompts and the step
runner live in the server_workflows shared module
(shared_modules/SHM-nznzhpp0).

Inputs (declared in OHK-69gk5ne9_metadata.json):
  workflow         (STR, required)  the workflow's SWF- id (its exact name also
                                    works). generate_options_for_workflow lists
                                    the enabled workflows whose filters match
                                    the server.
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
workflow against the server, every step's action against the server, and
every value against its input before the first step runs. With several
servers selected, each server gets its own child job, which runs this
plug-in for that one server, so each server's step outputs stay apart.
"""
import json

from common.methods import set_progress
from jobs.models import Job
from utilities.logger import ThreadLogger

from shared_modules import server_workflows as swf

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


def generate_options_for_workflow(field=None, server=None, servers=None, **kwargs):
    """The enabled workflows offered on the server, or on every server of a bulk run."""
    return swf.workflow_options(_targets(server, servers))


def _runner_hook(job):
    """This plug-in, from the job that is running it."""
    params = job.job_parameters.cast()
    hook = getattr(params, "hook", None)
    if hook is None:
        action = getattr(params, "action_type", None)
        hook = getattr(action, "hook", None)
    return hook.cast() if hook is not None else None


def _run_per_server(job, workflow, targets, values):
    """One child job per server, all started at once; returns the combined result."""
    hook = _runner_hook(job)
    if hook is None:
        return "FAILURE", "", "Could not find the Run Workflow plug-in from this job."
    payload = json.dumps(values)
    children = []
    for target in targets:
        jobs = hook.run_as_job(
            owner=job.owner,
            servers=[target],
            parent_job=job,
            context={"workflow": workflow["id"], "workflow_inputs": payload},
        )
        if not jobs:
            return "FAILURE", "", f"CloudBolt did not start the workflow on {target.hostname}."
        children.append((target, jobs[0]))
    set_progress(f"Workflow '{workflow['name']}' started on {len(children)} servers, one job each.")
    Job.wait_for_jobs([child for _, child in children])
    lines, failures, warnings = [], [], []
    for target, child in children:
        child = Job.objects.get(pk=child.pk)
        lines.append(f"{target.hostname}: {child.status} (job {child.id})")
        if child.status == "WARNING":
            warnings.append(target.hostname)
        elif child.status != "SUCCESS":
            failures.append(target.hostname)
    output = "\n".join(lines)
    if failures:
        return "FAILURE", output, f"The workflow did not succeed on {', '.join(failures)}."
    if warnings:
        return "WARNING", output, f"The workflow ended with warnings on {', '.join(warnings)}."
    return "SUCCESS", output, ""


def run(job=None, server=None, servers=None, workflow=None, workflow_inputs=None, **kwargs):
    targets = _targets(server, servers)
    if not targets:
        return "FAILURE", "", "Run Workflow needs at least one server."

    try:
        chosen = swf.resolve_workflow(workflow)
        if not chosen.get("enabled", True):
            raise swf.WorkflowError(f"Workflow '{chosen['name']}' is disabled.")
        steps = swf.load_steps(chosen)
        values = swf.parse_prompt_values(chosen, steps, workflow_inputs)
        problems = swf.preflight(chosen, steps, targets, values)
    except swf.WorkflowError as err:
        return "FAILURE", "", str(err)
    if problems:
        return "FAILURE", "", "The workflow cannot start:\n" + "\n".join(problems)

    set_progress(
        f"Workflow '{chosen['name']}': {len(steps)} step(s) on {len(targets)} server(s): "
        + ", ".join(step.name for step in steps)
    )
    if len(targets) > 1:
        return _run_per_server(job, chosen, targets, values)

    status, output, errors, outputs = swf.run_workflow(chosen, targets[0], values, job=job)
    return {"status": status, "output_message": output, "error_message": errors, "outputs": outputs}
