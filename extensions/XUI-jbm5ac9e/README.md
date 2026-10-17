# Server Workflows (XUI)

Adds **Admin > Custom Extensions > Server Workflows**, where CB Admins build the workflows that the [Run Workflow](../../server_actions/SVA-j7rbsh39/) server action runs on a server: a name, filters, and an ordered list of steps, each an existing Remote Script, CloudBolt plug-in, webhook, email or copy-file action with its inputs set to a fixed value, asked for when the workflow runs, or taken from an earlier step's output.

The step picker offers only actions that can run on every server the workflow's filters allow, and lists the others with the reason. Copy makes a disabled copy for review. The page needs the `server_workflows` shared module, which arrives with Run Workflow, so sync that action (or `packages/PKG-v20qv308`) first, then restart the web server (`systemctl restart httpd`), because the extension adds URL paths. The same operations are available as JSON under `/xui/server_workflow_builder/api/` (CB Admin, session auth with CSRF). Everything requires CB Admin.

## Actions tab

CloudBolt's UI creates a Remote Script or plug-in only inside a server, resource or orchestration action, and deleting that action deletes the script too. The Actions tab creates them on their own:

- **New Remote Script** (Windows or Linux) builds the script from Server Task Template (Windows) or (Linux) on the appliance: the template's helpers and error handling, one input line per declared input, and your task body. Run Task then offers it on every server its OS families, groups and environments allow.
- **New plug-in** builds a CloudBolt Plug-in a workflow step can run on a server.
- Inputs take a type, required, options and a regex pattern, saved as CloudBolt's regex constraint (matched from the start, on string and multi-line text inputs). Password inputs are not offered, because CloudBolt logs Run Task's and Run Workflow's inputs in plain text.

The list shows each Remote Script (CloudBolt's own included), plug-in, webhook, email and copy-file action with its filters, whether Run Task offers it, and what uses it (workflows, server, resource and orchestration actions, blueprints and the rest of CloudBolt's dependents). **Check a server** shows which actions can run on one server. Edit and delete on CloudBolt's own action page, linked from each row.

The same list and create calls are at `/xui/server_workflow_builder/api/base-actions/` (GET; POST with `"preview": true` returns the file without creating it).
