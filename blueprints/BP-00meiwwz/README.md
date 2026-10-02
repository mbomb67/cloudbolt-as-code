# HCP Terraform No-Code Module

Provisions infrastructure through HCP Terraform's no-code provisioning workflow. One blueprint targets one pinned no-code registry module. The orderer picks a CloudBolt Environment (its Azure subscription and tenant become the workspace's `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID`) and fills the module's variables; dropdowns can list the allowed values an admin defined on the module in HCP, or values from the environment. The build plugin creates a dedicated workspace from the module and pauses the job for human plan review before anything is applied. Sibling of the VCS-based HCP Terraform VM blueprint (BP-b0qm83lh).

Deployed resources are of type **HCP Terraform Workspace**.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-axtt0yqq | HCP Terraform No-Code Module |
| Teardown | OHK-y9d1uwhw | Teardown HCP Terraform No-Code Module |
| Discovery | OHK-b1n02ula | Discover HCP Terraform No-Code Workspaces |
| Options hook | HPA-swe1kifa | Generate options for HCP Terraform coordinates (hook OHK-529jjzli) |
| Day-2 action | RSA-dxrh4m6j | Update Variables (the shared Terraform Update action: hook OHK-lvy5tj0y, form FRM-h4py5w3a) |
| Day-2 action | RSA-pngq92ss | Deploy Latest Version (hook OHK-4y8f1vff) |
| Shared module | SHM-jlguerjr | tfc_api |
| Shared module | SHM-r0oq14r7 | env_options |
| Webhook | IWH-yj93is5z | Form Options (hook OHK-fx500o2r) |
| Form | FRM-1dxfulvq | HCP Terraform No-Code Module order form |

## Prerequisites
- An HCP Terraform organization on an edition that includes no-code provisioning (HashiCorp lists Standard and Premium; not the free tier), and a project with a project-scoped variable set holding the service principal credentials (not flagged priority; the principal needs a role in every target subscription).
- A module in the organization's private registry that declares its own providers, with no-code provisioning enabled and a version pinned, and its `nocode-*` ID. Define variable options on the module in HCP for any variable you want as a dropdown. A ready-made one is [docs/examples/terraform/azure-vm-nocode](../../docs/examples/terraform/azure-vm-nocode/README.md); the shipped form is authored for it.
- A team or user API token in the `password` field of a ConnectionInfo labeled `tf-cloud`. Organization tokens are rejected by the no-code endpoints.
- CloudBolt Environments on Azure resource handlers, entitled to the ordering groups.
- A `cb_admin` user to approve plans.

Full walkthrough: [../../docs/hcp-no-code-setup.md](../../docs/hcp-no-code-setup.md); shared mechanics (ConnectionInfo, approval gate, recovery) are in [../../docs/hcp-terraform-setup.md](../../docs/hcp-terraform-setup.md).

## Setup
1. Pin the coordinates as the blueprint's parameters (Blueprint > Parameters, destination Resource, exactly one option each): `tfc_connection_info` (`CON-…`), `tfc_organization`, `tfc_project`, `tfc_nocode_module_id` (`nocode-…`). Each parameter carries the Generate options for HCP Terraform coordinates action, so **Add option** offers the `tf-cloud` connections, then the organizations the pinned connection sees, then its projects and no-code-enabled registry modules; remove the `FILL-ME` placeholder and pin in that order. A single option is hidden from the order form and written onto each deployment's resource before the build plugin runs, which is where the build, discovery, webhook and day-2 code read it. A sync re-creates the options from this metadata (`parameters[].options`), so export the blueprint back to the repo (or edit `options`) after pinning in the UI. The build plugin refuses to run while any value contains `FILL-ME`.
2. The form's Module Variables panel ships authored for the sample module (`vm_size` lists the variable options defined on the module in HCP; resource group, subnet and image come from the environment). For another module, replace the fields with one per input variable, named exactly as the variable. For HCP-defined options use the Form Options webhook with `source=tfc_variable_options&service_item=BDI-t474vto9&variable=<name>` (it reads the connection and module ID from the blueprint's pinned parameters server-side); for environment-derived values use `source=resource_group|subnet|vm_size|os_image|location|cf:<field>`. List sensitive variables in the hidden `_sensitive` field; use a key/value matrix for map variables. Keep the naming-only `deployment_name` field.
3. Sync the repo, then restart CloudBolt so the shared modules are reloaded. Inbound webhooks sync separately from a blueprint: after a change to the Form Options webhook or its plugin, sync `webhooks/` explicitly, or the form keeps calling the old plugin.
4. Re-enter the token in every `tf-cloud` ConnectionInfo after each sync.

## Notes
- The no-code create carries the variables and the `ARM_*` environment variables, so the auto-queued first run already targets the chosen subscription; the build plugin adopts that run into the approval pause. Continue Job applies; canceling discards the run and leaves the resource `PROVFAILED` with its workspace ID stored.
- Update Variables is the shared Terraform Update action: a form built from this blueprint's order form and pre-filled with the deployment's current values. Unknown and sensitive keys are rejected, a blank field keeps its value, and it fails fast if the workspace has a pending run.
- Deploy Latest Version upgrades a deployment's workspace to the module version pinned in HCP Terraform (HCP's workspace-upgrade API), with the same plan-approval pause. It is idempotent: a deployment already on the pinned version reports that and creates no run. It is bulk-safe: selecting several deployments in the resource list runs them in one job, one after another, pausing once per deployment that needs the upgrade; a failure on one does not stop the others. If the new version adds a required variable, the plan fails; set it on the workspace in HCP Terraform and run the action again. Moving the pin itself is an HCP step ([docs/hcp-no-code-setup.md section 9](../../docs/hcp-no-code-setup.md#9-module-version-changes)).
- If a jobengine restart kills a paused job, the orphaned TFC run blocks the workspace; discard it from the resource's Terraform tab, in TFC, or delete the resource.
- Teardown fails fast if another live CloudBolt job owns the resource; otherwise it discards orphaned runs, runs an auto-confirmed destroy, and safe-deletes the workspace. A retry re-adopts the workspace by its `cb-nc-<resource global ID>` name.
- Onboarding another module means cloning this blueprint, pinning its own coordinates on the Parameters tab, and authoring a new form for its variables. Freeze the build plugin's `action_inputs` before authoring the form.
- The HCP Terraform Workspace extension ([XUI-ax1sluwi](../../extensions/XUI-ax1sluwi/)) adds Terraform and Terraform Variables tabs to these resources: workspace state, the module's name and the version the workspace runs with an update-available notice that links to Deploy Latest Version, run history, pending-run discard, managed resources, and read-only variables.
- Sync Resources (the blueprint's button or CloudBolt's built-in Sync Resources recurring job) runs the discovery plugin: it lists the organization's workspaces, keeps those whose `source-module-id` names the pinned module (any version, any project), and refreshes each matching resource's workspace name, module version, update-available flag, latest final run, `tfc_output_*` and `tfc_var_*` values. A workspace with no resource is onboarded (group and owner from the resource its `cmp:resource-id` tag or `cb-nc-` name points at, else Unassigned); a resource whose workspace returns 404 is marked Historical. Sensitive variable values are never read. The coordinates are the blueprint's single-option parameters. Keep `auto_historical_resources` off: it would also retire deployments that are still provisioning.
- A sync never renames an existing resource type: the importer returns the type it finds by ID or name unchanged. The label here ("HCP Terraform Workspace") must be applied once on the instance (Admin > Resource Types, or `PATCH /api/v3/cmp/resourceTypes/RT-nbw86jvn/` with `label` and `pluralLabel`); the metadata then matches what the next export writes.
