# HCP Terraform VM

Provisions a VM through HCP Terraform (TFC) using one dedicated, VCS-backed TFC workspace per deployment. The order form collects the Terraform template's variables; the build plugin creates the workspace, writes the variables, runs a plan, and pauses the job for human review before anything is applied. HCP Terraform owns state and the cloud credentials, so no CloudBolt environment or resource handler appears on the order form.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-pvo05e24 | HCP Terraform VM |
| Teardown | OHK-2b9qu490 | Teardown HCP Terraform VM |
| Day-2 action | RSA-dxrh4m6j | Terraform Update (hook OHK-lvy5tj0y) |
| Day-2 action | RSA-e59s1v24 | Resize (hook OHK-9xffkz53) |
| Shared module | SHM-jlguerjr | tfc_api |
| Form | FRM-t3v8zpb7 | HCP Terraform VM order form |

## Prerequisites
- An HCP Terraform organization and project, with a project-scoped variable set holding the cloud credentials (the shipped form targets an Azure template), and a VCS provider connected to the Terraform repo. CloudBolt never creates these.
- A team API token stored in the `password` field of a CloudBolt ConnectionInfo labeled `tf-cloud` (host in the IP field, port 443, `https`). The label, not the name, is how the integration finds it.
- A Terraform repo whose `variables.tf` matches the form's Template Variables panel (`vm_name`, `resource_group_name`, `subnet_id`, `vm_size`, `admin_username`, `admin_password`, `os_image`, `tags`), or a re-authored panel.
- A `cb_admin` user to approve plans: only administrators can resume a paused job.

Full walkthrough: [../../docs/hcp-terraform-setup.md](../../docs/hcp-terraform-setup.md).

## Setup
1. In `shared_modules/SHM-jlguerjr/SHM-jlguerjr_script.py`, replace the `CLOUDBOLT_PORTAL_URL` `FILL-ME` value with this instance's base URL. Every TFC-backed job fails fast while the placeholder remains.
2. In `BP-b0qm83lh_metadata.json`, edit the build item's `parameter_defaults` (the `*_a352` entries): `tfc_connection_info` (the `tf-cloud` ConnectionInfo global ID, `CON-...`), `tfc_organization`, `tfc_project`, `tfc_repo_identifier` (`org/repo`), and `tfc_branch` (ships `main`). The build plugin refuses to run while any value contains `FILL-ME`. The working directory defaults to the repo root; add a `tfc_working_directory` default to pin a subdirectory.
3. If your template's variables differ, edit the form's Template Variables panel so each field name equals a Terraform variable name. List sensitive variables in the hidden `_sensitive` field (ships `["admin_password"]`); they are written to TFC as sensitive and never mirrored into custom fields.
4. Sync the repo, then restart CloudBolt so the updated shared module is reloaded.
5. Re-enter the team token in every `tf-cloud` ConnectionInfo after each sync. Export redacts secrets, so the token does not travel through the repo.

## Notes
- Approval gate: the job pauses after `terraform plan` with add/change/destroy counts, a warnings block, a plan excerpt, and the TFC run URL. Continue Job approves and applies; canceling the job discards the run (provision ends `PROVFAILED` with its workspace ID stored; day-2 reverts the workspace variables). The wait is bounded by the global `job_timeout` preference (default 8 hours).
- Terraform Update edits values of variables the deployment already manages; unknown keys are rejected. Resize changes only `vm_size`. Both fail fast if the workspace has a pending run.
- Teardown runs an auto-confirmed destroy (no plan review), then safe-deletes the workspace. A missing or already-deleted workspace is a WARNING, not a failure, so PROVFAILED resources clean up.
- Every non-sensitive Terraform output is recorded on the resource as `tfc_output_<name>`; mark secret-bearing outputs `sensitive = true` in the template.
- If a jobengine restart kills a paused job, the orphaned TFC run blocks the workspace; discard it in TFC or delete the resource (teardown discards orphaned runs).
