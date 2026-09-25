# HCP Terraform VM

Provisions a VM through HCP Terraform (TFC) using one dedicated, VCS-backed TFC workspace per deployment. The orderer picks a CloudBolt Environment; its Azure subscription and tenant are passed to the workspace as `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID`, and the form's resource group, subnet, size and image dropdowns are filled from that environment. The build plugin creates the workspace, writes the variables, runs a plan, and pauses the job for human review before anything is applied. HCP Terraform holds the credentials; the resource handler is never shown to the user.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-pvo05e24 | HCP Terraform VM |
| Teardown | OHK-2b9qu490 | Teardown HCP Terraform VM |
| Day-2 action | RSA-dxrh4m6j | Terraform Update (hook OHK-lvy5tj0y and form FRM-h4py5w3a, shared by every HCP Terraform blueprint) |
| Day-2 action | RSA-e59s1v24 | Resize (hook OHK-9xffkz53) |
| Shared module | SHM-jlguerjr | tfc_api |
| Shared module | SHM-r0oq14r7 | env_options |
| Webhook | IWH-yj93is5z | Form Options (hook OHK-fx500o2r) |
| Form | FRM-t3v8zpb7 | HCP Terraform VM order form |

## Prerequisites
- An HCP Terraform organization and project, with a project-scoped variable set holding the service principal's `ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_TENANT_ID`, `ARM_SUBSCRIPTION_ID`. Not flagged priority. The principal needs a role in every subscription the CloudBolt environments target.
- A VCS provider connected to the Terraform repo.
- A team API token in the `password` field of a ConnectionInfo labeled `tf-cloud` (host in the IP field, port 443, `https`).
- A Terraform repo whose `variables.tf` matches the form (`vm_name`, `resource_group_name`, `subnet_id`, `vm_size`, `admin_username`, `admin_password`, `os_image`, `tags`). The azurerm provider reads `ARM_*` from the environment; derive `location` from the subnet's VNet.
- CloudBolt Environments on Azure resource handlers, entitled to the ordering groups, with resource groups and subnets imported, VM sizes enabled and OS builds available.
- A `cb_admin` user to approve plans.

Full walkthrough: [../../docs/hcp-terraform-setup.md](../../docs/hcp-terraform-setup.md).

## Setup
1. In `forms/FRM-t3v8zpb7`, set the `defaultValue` of each hidden `plugin-bdi-lwys1ug9.<name>` field: `tfc_connection_info` (`CON-…`), `tfc_organization`, `tfc_project`, `tfc_repo_identifier` (`owner/repo`), `tfc_branch` (ships `main`); add a hidden `tfc_working_directory` field to pin a subdirectory. The form is the only place these are pinned (the build item carries no `parameter_defaults`, which a custom form would not receive anyway). The build plugin refuses to run while any value contains `FILL-ME`.
2. If your template's variables differ, edit the form's Template Variables panel so each field name equals a Terraform variable name. Environment-derived fields use the Form Options webhook (`source=resource_group|subnet|vm_size|os_image|location|cf:<field>`). List sensitive variables in the hidden `_sensitive` field.
3. Sync the repo, then restart CloudBolt so the shared modules are reloaded.
4. Re-enter the team token in every `tf-cloud` ConnectionInfo after each sync.

## Notes
- Approval gate: the job pauses after `terraform plan` with add/change/destroy counts, a warnings block, the plan in Terraform CLI style with attribute-level diffs (needs workspace admin on the team token; otherwise resource actions only, plus a hint naming the permission to grant), and the TFC run URL. Continue Job applies; canceling discards the run (resource ends `PROVFAILED` with its workspace ID stored). The wait is bounded by the global `job_timeout` preference.
- The environment is re-checked server-side at run time against `group.get_available_environments()`; the webhook checks group membership and environment entitlement on every call.
- Terraform Update opens a form built from this blueprint's order form: the same fields, scoped to the environment the deployment was ordered into and pre-filled with its current values; a blank field keeps its value and sensitive variables are not shown. Resize changes only `vm_size`. Both fail fast if the workspace has a pending run.
- Teardown runs an auto-confirmed destroy, then safe-deletes the workspace. A missing workspace is a WARNING, so PROVFAILED resources clean up.
- Every non-sensitive Terraform output is recorded on the resource as `tfc_output_<name>` and shown on its Overview; the submitted variables are recorded as `tfc_var_<name>` on its Parameters tab. Mark secret-bearing outputs `sensitive = true`.
- If a jobengine restart kills a paused job, the orphaned TFC run blocks the workspace; discard it from the resource's Terraform tab, in TFC, or delete the resource.
- The HCP Terraform Workspace extension ([XUI-ax1sluwi](../../extensions/XUI-ax1sluwi/)) adds Terraform and Terraform Variables tabs to these resources: workspace state, run history, pending-run discard, managed resources, and read-only variables.
