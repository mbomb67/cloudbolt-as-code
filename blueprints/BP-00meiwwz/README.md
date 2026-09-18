# HCP Terraform No-Code Module

Provisions infrastructure through HCP Terraform's no-code provisioning workflow. One blueprint targets one pinned no-code registry module; a static order form collects that module's input variables; the build plugin creates a dedicated workspace from the module, writes the variables, and pauses the job for human plan review before anything is applied. Sibling of the VCS-based HCP Terraform VM blueprint (BP-b0qm83lh); the two share only the `tfc_api` shared module.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-axtt0yqq | HCP Terraform No-Code Module |
| Teardown | OHK-y9d1uwhw | Teardown HCP Terraform No-Code Module |
| Day-2 action | RSA-qofayikp | Update Variables (hook OHK-oj87ukle) |
| Shared module | SHM-jlguerjr | tfc_api |
| Form | FRM-1dxfulvq | HCP Terraform No-Code Module order form |

## Prerequisites
- An HCP Terraform organization and project with a project-scoped variable set holding the cloud credentials.
- A module in the organization's private registry with no-code provisioning enabled, and its `nocode-*` ID (not shown prominently in the UI; see the runbook for how to find it).
- A team or user API token in the `password` field of a ConnectionInfo labeled `tf-cloud`. Organization tokens are rejected by the no-code endpoints.
- A `cb_admin` user to approve plans.

Full walkthrough: [../../docs/hcp-no-code-setup.md](../../docs/hcp-no-code-setup.md); shared mechanics (ConnectionInfo, approval gate, recovery) are in [../../docs/hcp-terraform-setup.md](../../docs/hcp-terraform-setup.md).

## Setup
1. Replace the `CLOUDBOLT_PORTAL_URL` `FILL-ME` value in `shared_modules/SHM-jlguerjr/SHM-jlguerjr_script.py` if not already done for the VM blueprint.
2. In `BP-00meiwwz_metadata.json`, edit the build item's `parameter_defaults`: `tfc_connection_info` (ConnectionInfo global ID, `CON-...`), `tfc_organization`, `tfc_project` (project name), and `tfc_nocode_module_id`. The build plugin refuses to run while any value contains `FILL-ME`.
3. Author the form (`forms/FRM-1dxfulvq`): in the Module Variables panel, replace the shipped example fields with one field per module input variable, named exactly as the variable. List sensitive variables in the hidden `_sensitive` field; use a key/value matrix for map variables (written as HCL). Keep the naming-only `deployment_name` field; it names the CloudBolt resource and is not sent to Terraform.
4. Sync the repo, then restart CloudBolt so the updated shared module is reloaded.
5. Re-enter the token in every `tf-cloud` ConnectionInfo after each sync; export redacts secrets.

## Notes
- The no-code create auto-queues the first run; the build plugin adopts it into the approval pause. Continue Job applies; canceling discards the run and leaves the resource `PROVFAILED` with its workspace ID stored.
- Update Variables edits values only. Unknown keys and sensitive keys are rejected (edit sensitive values in the TFC workspace UI). It fails fast if the workspace has a pending run.
- There is no module-version upgrade action. To move to a new version, re-pin the module version in HCP or tear down and re-order.
- Teardown fails fast if another live CloudBolt job owns the resource; otherwise it discards orphaned runs, runs an auto-confirmed destroy, and safe-deletes the workspace. Missing or already-deleted workspaces are a WARNING. A retry after a crash re-adopts the workspace by its `cb-nc-<resource global ID>` name.
- Onboarding another module means cloning this blueprint, re-pinning its coordinates, and authoring a new form. Freeze the build plugin's `action_inputs` before authoring the form; the form hardcodes the panel funnel name.
