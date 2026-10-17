# Run Workflow

Runs a server workflow on a server: a named, ordered list of existing CloudBolt actions (Remote Scripts, CloudBolt plug-ins, webhooks, emails, copy-file actions) that run one after another as one job, with outputs passed from step to step. Admins build workflows on the Server Workflows admin page; users pick one from the workflows that fit the server and fill in only what the workflow asks for.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-69gk5ne9 | Run Workflow |
| Custom form | FRM-ecw50rd0 | Run Workflow |
| Form function | FJS-kn5lr5am | serverWorkflowForm |
| Shared module | SHM-nznzhpp0 | server_workflows |
| Shared module | SHM-571l7249 | server_tasks (from [Run Task](../SVA-4sin3s0q/), runs the script steps) |
| Inbound webhook | IWH-xjlrmvc8 | Server Workflows (`server-workflows`), called by the form |
| UI extension | XUI-jbm5ac9e | Server Workflows admin page |

## Setup
1. Sync the Server Automation package (`packages/PKG-v20qv308`), or sync this action, `webhooks/IWH-xjlrmvc8` and `extensions/XUI-jbm5ac9e`. A server action sync does not bring the webhook or the extension.
2. Restart the web service (`systemctl restart httpd`) after the first sync and after any change to `server_workflows` or `server_tasks`: the admin page, the form's webhook and the workflow list run in the web process, which keeps the old shared modules until it restarts, and the extension adds URL paths.
3. Give the groups that should run workflows permission to run this server action. Approval, scheduling and bulk runs are this action's own settings.

## Building a workflow
Admin > Custom Extensions > Server Workflows > Add workflow.
- **Filters**: OS families, resource technologies, environments, groups, OS builds and configuration managers. Run Workflow offers the workflow only on servers that pass every filter set, with the checks CloudBolt makes for actions (own group only, an OS family covers its children). OS builds is extra: actions have no such filter.
- **Steps**: Add step lists only the actions that can run on every server the filters allow; the rest are listed apart with the reason. Remote Scripts always name OS families, so set an OS family (or OS build) filter before adding one. Reorder by dragging or with the arrows.
- **Each input** is set one of three ways:
  - *Ask when run*: the Run Workflow form asks for it (the default).
  - *Fixed value*: saved with the workflow, never asked.
  - *Output of a step*: an output of an earlier step in the same run.
- **Each step** has a key (names its outputs), Continue on error, Retries with a delay, and, for non-script steps, a timeout.
- **Files** (script steps): files picked from the File Archives content, staged on the server before the script runs, as `archive/file`, `archive/file@N` and optionally `extract`. Without File Archives installed a step cannot have files.

Password and object-type inputs (PWD, ETXT, networks, files, ...) can be neither asked for nor saved: CloudBolt writes this action's form payload to the job log, and the workflow store is plain text. A step whose action requires one cannot be added.

## Outputs
- A Remote Script publishes an output with a `CB_OUTPUT name=value` line, the convention of [Run Task](../SVA-4sin3s0q/): `Write-TaskOutput name value` (PowerShell) or `task_output name value` (bash) in its base scripts. Print them last and exit 0: WinRM drops a script's output when it exits non-zero, and Azure Run Command keeps only the last 4 KB.
- Every script step also publishes `stdout`, the last 4,000 characters of its output, and one `archive_<file stem>_version` output per attached file.
- A plug-in step publishes the `outputs` dict it returns.
- Outputs are saved on the workflow's job under each step's key (visible on the job page), and later plug-in steps also receive them in their `outputs` keyword argument.

## Running
- **UI**: Actions > Run Workflow on a server. The form lists the workflow's steps, asks for its prompts (dropdowns use the input's options, or a plug-in's generated options for that server), and offers to save credentials on a server that has none for a script step.
- **API and MCP**: run the action with `workflow` (the `SWF-` id or exact name) and `workflow_inputs`, a JSON object `{"<step key>": {"<input>": value}}`.
- Everything is checked before the first step: the workflow and every step's action against each server, every value against its input, and credentials for script steps. A failed step stops the run, unless it continues on error, which ends the run with WARNING. Cancelling the job stops it before the next step.
- Remote Scripts run inside the workflow's job, with their own timeout, sudo and retry settings, so their output can be read; other actions run as child jobs.
- With several servers selected, each server gets its own child job, all started at once.

## From a blueprint
The Run Workflow plug-in is also a blueprint build item: add it after the server tier, as [Server with Workflow](../../blueprints/BP-vy9ytey6/) does. It runs on the servers of the order's resource.
- **Pinned**: give the item's Workflow input a default (the workflow's name), which hides it on the order form.
- **User's choice**: leave it without a default and the order form lists every enabled workflow; Workflow inputs pre-fills with the chosen workflow's prompts.
- A prompt left empty takes a same-named parameter of the resource, else of the server.
- The item's outputs are the run's step outputs, for later build items.

## Notes
- Workflows are stored on the appliance, one value each of the `server_workflow_store` parameter in the `server_workflows` namespace. They are not repository content; deleting that parameter deletes every workflow.
- CloudBolt does not validate a custom-form submit, so the plug-in rechecks everything itself.
- Group filters match the server's own group, as action filters do; parent groups do not count.
