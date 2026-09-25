# HCP Terraform Workspace (XUI)

Adds two tabs to resources provisioned by the HCP Terraform blueprints ([BP-b0qm83lh](../../blueprints/BP-b0qm83lh/) and [BP-00meiwwz](../../blueprints/BP-00meiwwz/)):

- **Terraform**: workspace summary (lock, Terraform version, execution mode, source repo or no-code module, drift assessment, latest cost estimate), a banner for pending runs with a **Discard** button, the run history with plan add/change/destroy counts and a link to the CloudBolt job that created each run, and the resources Terraform manages.
- **Terraform Variables**: read-only workspace variables and the variable sets the workspace inherits. Sensitive values are never shown.

Editing variables stays in the blueprints' resource actions (Terraform Update, Resize, Update Variables), so every change goes through a plan-approval job.

## Prerequisites
- CloudBolt 8.6 or later.
- Resources that carry `tfc_workspace_id`, `tfc_organization` and `tfc_connection_info` (stamped by the blueprints' build plugins). The tabs only appear on those resources.
- The `tfc_api` shared module ([SHM-jlguerjr](../../shared_modules/SHM-jlguerjr/)) synced to the same instance. The tabs read HCP Terraform with the resource's `tf-cloud` ConnectionInfo, so its team token needs read access to the workspace's runs, variables and resources (the blueprints' token already has it).

## Setup
1. Sync the repository, restart CloudBolt so the shared module is reloaded, and confirm the extension is enabled under Admin > Extensions.
2. Re-enter the team token in the `tf-cloud` ConnectionInfo after each sync (exports redact it); until then the panels show a configuration error.

## Notes
- Viewing needs the `resource.view` permission on the resource. Discard is offered to CloudBolt admins and users with `resource.manage_parameters` (change `DISCARD_PERMISSION` in `views.py` to use another permission). It is refused for a run that a running CloudBolt job still owns (cancel that job instead) and for runs that are not awaiting confirmation (cancel those in HCP Terraform).
- Drift needs HCP Terraform Standard or Premium with health assessments enabled; cost estimates need cost estimation enabled in the organization. Both show as not available otherwise.
- Panels load asynchronously and read HCP Terraform on every load; nothing is cached in CloudBolt. State files are never read: the resource list comes from the workspace-resources endpoint.
- Styling follows CloudBolt's own order and resource pages (theme tokens, Bootstrap 5, Bootstrap Icons) so Branded Themes apply.
