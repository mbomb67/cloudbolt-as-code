# HCP Terraform Workspace (XUI)

Adds two tabs to resources provisioned by the HCP Terraform blueprints ([BP-b0qm83lh](../../blueprints/BP-b0qm83lh/) and [BP-00meiwwz](../../blueprints/BP-00meiwwz/)):

- **Terraform**: takes the place of the built-in Overview tab on CloudBolt 2026.1.73 and later (on older versions it follows the built-in tabs). A row under the title shows the resource's owner and group (links for users who may open them), status, installed date, labels (with the edit button for users with `resource.manage_labels`) and rate. Below it, the workspace summary (lock, Terraform version, execution mode, source repo or no-code module, drift assessment, latest cost estimate), a banner for pending runs with a **Discard** button, the run history with plan add/change/destroy counts and a link to the CloudBolt job that created each run, and the resources Terraform manages. For a no-code workspace the Source cell shows the module's name, the version the workspace runs and its `nocode-…` ID; when HCP Terraform pins a different version, an **Update available** notice appears with a **Deploy Latest Version** button (shown to users allowed to run that action on the resource).
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
- Viewing needs the `resource.view` permission on the resource. Discard is offered to CloudBolt admins and users with `resource.manage_parameters` (change `DISCARD_PERMISSION` in `views.py` to use another permission). The Deploy Latest Version button appears when the action (`resource_actions/RSA-pngq92ss`) is synced, applies to the resource's blueprint, and the viewer may run it (`resource_action_permitted`). It is refused for a run that a running CloudBolt job still owns (cancel that job instead) and for runs that are not awaiting confirmation (cancel those in HCP Terraform).
- Drift needs HCP Terraform Standard or Premium with health assessments enabled; cost estimates need cost estimation enabled in the organization. Both show as not available otherwise.
- Panels load asynchronously and read HCP Terraform on every load; nothing is cached in CloudBolt. State files are never read: the resource list comes from the workspace-resources endpoint.
- The tabs share the look of the EKS, AKS, GKE, OpenShift and StormForge tabs: an unframed header and tables, compact summary tiles, and colors taken from CloudBolt's theme tokens (Bootstrap 5, Bootstrap Icons), so Branded Themes and dark mode apply.
