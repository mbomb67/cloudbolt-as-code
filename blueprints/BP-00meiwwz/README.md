# HCP Terraform No-Code Module

Provisions infrastructure through HCP Terraform's no-code provisioning workflow. One blueprint targets one pinned no-code registry module. The orderer picks a CloudBolt Environment (its Azure subscription and tenant become the workspace's `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID`) and fills the module's variables; dropdowns can list the allowed values an admin defined on the module in HCP, or values from the environment. The build plugin creates a dedicated workspace from the module and pauses the job for human plan review before anything is applied. Sibling of the VCS-based HCP Terraform VM blueprint (BP-b0qm83lh).

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-axtt0yqq | HCP Terraform No-Code Module |
| Teardown | OHK-y9d1uwhw | Teardown HCP Terraform No-Code Module |
| Day-2 action | RSA-qofayikp | Update Variables (hook OHK-lvy5tj0y, the Terraform Update plugin shared by every HCP Terraform blueprint) |
| Shared module | SHM-jlguerjr | tfc_api |
| Shared module | SHM-r0oq14r7 | env_options |
| Webhook | IWH-yj93is5z | Form Options (hook OHK-fx500o2r) |
| Form | FRM-1dxfulvq | HCP Terraform No-Code Module order form |

## Prerequisites
- An HCP Terraform organization and project with a project-scoped variable set holding the service principal credentials (not flagged priority; the principal needs a role in every target subscription).
- A module in the organization's private registry with no-code provisioning enabled, and its `nocode-*` ID. Define variable options on the module in HCP for any variable you want as a dropdown.
- A team or user API token in the `password` field of a ConnectionInfo labeled `tf-cloud`. Organization tokens are rejected by the no-code endpoints.
- CloudBolt Environments on Azure resource handlers, entitled to the ordering groups.
- A `cb_admin` user to approve plans.

Full walkthrough: [../../docs/hcp-no-code-setup.md](../../docs/hcp-no-code-setup.md); shared mechanics (ConnectionInfo, approval gate, recovery) are in [../../docs/hcp-terraform-setup.md](../../docs/hcp-terraform-setup.md).

## Setup
1. In `forms/FRM-1dxfulvq`, set the `defaultValue` of each hidden `plugin-bdi-t474vto9.<name>` field: `tfc_connection_info` (`CON-…`), `tfc_organization`, `tfc_project`, `tfc_nocode_module_id` (`nocode-…`). The form is the only place these are pinned (the build item carries no `parameter_defaults`, which a custom form would not receive anyway). The build plugin refuses to run while any value contains `FILL-ME`.
2. Author the form's Module Variables panel: replace the example fields with one field per module input variable, named exactly as the variable. For HCP-defined options use the Form Options webhook with `source=tfc_variable_options&service_item=BDI-t474vto9&variable=<name>` (it reads the connection and module ID from the form's hidden fields server-side); for environment-derived values use `source=resource_group|subnet|vm_size|os_image|location|cf:<field>`. List sensitive variables in the hidden `_sensitive` field; use a key/value matrix for map variables. Keep the naming-only `deployment_name` field.
3. Sync the repo, then restart CloudBolt so the shared modules are reloaded.
4. Re-enter the token in every `tf-cloud` ConnectionInfo after each sync.

## Notes
- The no-code create carries the variables and the `ARM_*` environment variables, so the auto-queued first run already targets the chosen subscription; the build plugin adopts that run into the approval pause. Continue Job applies; canceling discards the run and leaves the resource `PROVFAILED` with its workspace ID stored.
- Update Variables edits values only. Unknown keys and sensitive keys are rejected. It fails fast if the workspace has a pending run.
- There is no module-version upgrade action. Re-pin the version in HCP or tear down and re-order.
- Teardown fails fast if another live CloudBolt job owns the resource; otherwise it discards orphaned runs, runs an auto-confirmed destroy, and safe-deletes the workspace. A retry re-adopts the workspace by its `cb-nc-<resource global ID>` name.
- Onboarding another module means cloning this blueprint, authoring a new form with its own pinned coordinates and variables. Freeze the build plugin's `action_inputs` before authoring the form.
