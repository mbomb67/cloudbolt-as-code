"""
CloudBolt server action plug-in: Run Task (server_actions/SVA-4sin3s0q).

One button for every Remote Script that fits a server. The task dropdown lists
the Remote Scripts that can run on the selected server (or on every selected
server, for a bulk run); the chosen one runs there with its own rendering,
transport, timeout and credentials. The filters, the credential check and the
input handling live in the server_tasks shared module
(shared_modules/SHM-571l7249).

Inputs (declared in OHK-o4tgygjs_metadata.json):
  task         (STR, required)  the Remote Script's global ID (its name or
                                numeric id also work). generate_options_for_task
                                lists the ones that fit the server.
  task_inputs  (TXT, optional)  the task's own input values. The custom form
                                (forms/FRM-a5edrdj4) sends them as a Dynamic
                                Panel, which arrives as a list holding one
                                dict; the API, MCP and the built-in dialog send
                                a JSON object of input names and values. This
                                input is plain text in the job's parameters and
                                log, so it never carries a password: tasks
                                that need one cannot run from here.

A custom-form submit is not validated by CloudBolt, so this plug-in checks the
task against the server, and every input against the task's own definition,
before anything runs. Credentials come from the Remote Script, else the
server; when there are none (and no tech-specific execution without
credentials), the form saves them on the server through the Server Tasks
webhook first.
"""
from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules import server_tasks

logger = ThreadLogger(__name__)

# CloudBolt rebuilds a plug-in's inputs from the template variables in its
# source on every save and deletes the others. The line below names both
# inputs inside a block that never renders, so no submitted value is ever
# written into this code; run() reads the values from its kwargs.
# {% if False %}"{{ task }}" """{{ task_inputs }}"""{% endif %}


def _targets(server=None, servers=None):
    targets = list(servers or [])
    if not targets and server is not None:
        targets = [server]
    return targets


def generate_options_for_task(field=None, server=None, servers=None, **kwargs):
    """The Remote Scripts that can run on the server, or on every server of a bulk run."""
    return server_tasks.task_options(_targets(server, servers))


def run(job=None, server=None, servers=None, task=None, task_inputs=None, **kwargs):
    targets = _targets(server, servers)
    if not targets:
        return "FAILURE", "", "Run Task needs at least one server."

    try:
        hook = server_tasks.resolve_task(task)
        server_tasks.check_task(hook, targets)
        inputs = server_tasks.parse_task_inputs(hook, task_inputs)
    except server_tasks.TaskError as err:
        return "FAILURE", "", str(err)

    missing = [target.hostname for target in targets if server_tasks.needs_credentials(hook, target)]
    if missing:
        return (
            "FAILURE",
            "",
            f"CloudBolt has no credentials to log in to {', '.join(missing)}. Save them from the "
            "Run Task form, or set them on the server or on the Remote Script, and run the task again.",
        )

    set_progress(f"Task '{hook.name}' on {len(targets)} server(s), inputs: {', '.join(inputs) or 'none'}")
    outputs, failures = [], []
    for target in targets:
        set_progress(f"{target.hostname}: running '{hook.name}'")
        try:
            output = server_tasks.run_task(hook, target, inputs, job=job)
        except server_tasks.TaskError as err:
            failures.append(f"{target.hostname}: {err}")
            continue
        except Exception as err:
            logger.exception("Run Task '%s' failed on %s", hook.name, target.hostname)
            failures.append(f"{target.hostname}: {server_tasks.failure_text(err)}")
            continue
        output = server_tasks.tail(output)
        # WinRM returns the output without adding it to the job, unlike the other transports.
        if job is not None and target.is_windows() and not target.use_tech_specific_script_execution():
            job.set_progress(f"Output of '{hook.name}' on {target.hostname}", detailed_message=output)
        outputs.append(f"{target.hostname}:\n{output}" if len(targets) > 1 else output)
        set_progress(f"{target.hostname}: '{hook.name}' finished")

    output_text = "\n\n".join(outputs)
    if failures:
        return "FAILURE", output_text, "\n".join(failures)
    return "SUCCESS", output_text, ""
