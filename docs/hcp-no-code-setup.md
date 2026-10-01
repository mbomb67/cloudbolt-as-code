# HCP Terraform No-Code Module — Operator Setup

One-time setup for the **HCP Terraform No-Code Module** blueprint (`BP-00meiwwz`). It provisions through HCP Terraform's **no-code provisioning** workflow: one blueprint is pinned to one no-code registry module, a static order form (authored per blueprint) collects that module's input variables, and the build plugin creates one dedicated workspace per deployment from the module and pauses the job for human plan review before anything is applied.

The sibling runbook [hcp-terraform-setup.md](hcp-terraform-setup.md) covers the VCS-workspace blueprint (`BP-b0qm83lh`) and is the authority for everything the two share: the trust model (§1 there), organization / project / variable-set creation (§2–§4), the ConnectionInfo (§7), token re-entry after sync (§9), the approval gate (§10), recovery (§11) and plan-output hygiene (§12). This document links to it rather than restating it and covers what is different: the edition, the module, the registry, the no-code settings, the token type, and the form.

Follow §1–§5 in HCP Terraform, then §6 onward in this repo and CloudBolt.

## Prerequisites checklist

- [ ] An HCP Terraform organization on an edition that includes no-code provisioning. HashiCorp's module-design page states: "No-code provisioning is available in HCP Terraform Standard and Premium editions" ([source](https://developer.hashicorp.com/terraform/cloud-docs/no-code-provisioning/module-design)). The free tier that suffices for the VM blueprint does **not** — confirm the current edition matrix on the [pricing page](https://www.hashicorp.com/products/terraform/pricing) before starting.
- [ ] A **sandbox-only Azure subscription** and a service principal in it. [hcp-terraform-setup.md §1](hcp-terraform-setup.md#1-read-this-first-the-poc-trust-model) explains why this is a control, not a convenience; every word of it applies here.
- [ ] A Terraform **module** repository on a VCS host HCP Terraform supports, with admin rights on it (tags, branch protection, collaborator list) — or the ability to publish module versions through the API without a repository (§3). The sample module in [`examples/terraform/azure-vm-nocode/`](examples/terraform/azure-vm-nocode/README.md) is ready to copy into one.
- [ ] CloudBolt administrator (`cb_admin`) access on the target instance, with this repo already synced via Source Control Repos.
- [ ] At least one CloudBolt **Environment** on an Azure resource handler for the sandbox subscription, entitled to the ordering group. Resource groups, subnets, sizes and OS builds need importing only if the form reads them from the environment (§6).

---

## 1. Organization, project and credentials

Follow [hcp-terraform-setup.md](hcp-terraform-setup.md) §2 (organization), §3 (project) and §4 (project-scoped variable set) as written. Three no-code notes:

- The organization must be on an edition with no-code provisioning (checklist). A dedicated sandbox organization is still the recommendation, for the same blast-radius reason: the token CloudBolt holds is an Owners team token.
- This blueprint and the VM blueprint can share one project or use separate ones; the project name is pinned per blueprint (§6) either way. The credentials variable set must be **project-scoped** and must **not** be flagged *priority*: CloudBolt sends `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID` in the workspace create payload so the very first run already targets the chosen CloudBolt environment's subscription (§8), and a priority set would override them. The service principal needs a role in every subscription those environments point at.
- Create **no workspaces**. CloudBolt creates one per deployment from the module, named `cb-nc-<resource global ID>`; never pre-create or hand-edit them.

## 2. Author the module for no-code provisioning

Docs: [Design no-code ready modules](https://developer.hashicorp.com/terraform/cloud-docs/no-code-provisioning/module-design)

A no-code module is deployed as the **root** of its workspace — there is no wrapping root configuration — so it must be self-sufficient. A ready-made module that follows every rule below is in [`examples/terraform/azure-vm-nocode/`](examples/terraform/azure-vm-nocode/README.md) (an Azure VM on an existing subnet); the shipped order form is authored for it, so the quickest path is to copy that folder into its own repository, tag `v1.0.0`, and continue with §3. HashiCorp's requirements, plus the conventions this blueprint relies on:

1. **Standard module structure** with the resources in the repository root (`main.tf`, `variables.tf`, `outputs.tf`); submodules and examples in their usual folders.
2. **Providers are declared in the module.** Per the docs, "A no-code ready module must declare the required provider(s) directly in the module": the `terraform { required_providers { azurerm = { … } } }` block **and** the `provider "azurerm" { features {} }` block live in the module itself — the parts a normal child module leaves to its caller.
3. **No subscription or tenant variables**, and no `subscription_id` in the provider block. The azurerm provider reads `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID` from its environment; CloudBolt writes them per workspace from the chosen environment, and the client credentials come from the project's variable set (§1).
4. **Defaults where they make sense.** HashiCorp recommends "setting reasonable defaults when possible"; a form field left blank falls back to the module default. For a variable without a default that should be a pick-list, define its allowed values when you enable no-code (§4) — the order form renders them as a live dropdown. Variable **names are the form's field names**, so keep them stable; renaming a variable means editing the form.
5. **Outputs.** An output named `name` (or `vm_name`) names the CloudBolt resource; otherwise the form's *Deployment Name* is used. Every non-sensitive output in the applied state is recorded on the resource as a plaintext `tfc_output_<name>` custom field, so **mark any secret-bearing output `sensitive = true`** — mandatory, see [hcp-terraform-setup.md §12](hcp-terraform-setup.md#12-keep-secrets-out-of-plan-output).
6. **Tag a release.** The registry imports versions from semantic-version tags (`x.y.z`, optionally `v`-prefixed); tags in any other format are ignored ([publishing docs](https://developer.hashicorp.com/terraform/cloud-docs/registry/publish-modules)).

Lock the module repository down the same way as the VM template repo ([hcp-terraform-setup.md §5.4](hcp-terraform-setup.md#5-connect-the-vcs-provider-and-lock-down-the-template-repo)): write access to a published module version's source is access to the credentials at plan time, and the version pin (§4) means a re-tagged version rides into every future run.

## 3. Publish the module to the private registry

Docs: [Publish private modules](https://developer.hashicorp.com/terraform/cloud-docs/registry/publish-modules); API: [Registry modules](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/private-registry/modules)

Publishing needs the organization permission **Manage private registry** (the Owners team has it); through the API, **Manage modules**.

1. Connect the VCS provider as in [hcp-terraform-setup.md §5](hcp-terraform-setup.md#5-connect-the-vcs-provider-and-lock-down-the-template-repo) and grant it access to the module repository.
2. In HCP Terraform open **Registry**, then **Publish → Module**. Pick the VCS provider and the repository. Keep the default tag-based publishing: every semver tag becomes a module version.
3. On the **Add Module** screen tick **Add Module to no-code provision allowlist**, then **Publish module**. (Skipping the box is fine; §4 enables it afterwards.)
4. Note the module's registry ID (`mod-…`) if you will use the API in §4. It is the `id` of the module in the registry-modules listing:

   ```powershell
   $h = @{ Authorization = "Bearer $env:TFC_TOKEN" }
   (Invoke-RestMethod -Headers $h "https://app.terraform.io/api/v2/organizations/<org>/registry-modules").data | Select-Object id, @{n='name';e={$_.attributes.name}}, @{n='provider';e={$_.attributes.provider}}
   ```

Modules without a repository: the API can create a registry module with no backing VCS repository and accepts explicitly uploaded versions (see the API reference above). From §4 on, nothing differs.

## 4. Enable no-code provisioning, pin the version, define variable options

Docs: [Provision no-code infrastructure](https://developer.hashicorp.com/terraform/cloud-docs/no-code-provisioning/provisioning); [No-Code Provisioning API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/no-code-provisioning); tutorial: [Create and use no-code ready modules](https://developer.hashicorp.com/terraform/tutorials/cloud/no-code-provisioning)

Enabling no-code pins the **No-code Ready** designation to **one module version**: every CloudBolt deployment provisions exactly that version until you move the pin (§9). Variable options are defined against that pinned version.

**UI.** Open the module in the organization's Registry, click **Configure Settings**, then **Edit versions and variable options**: pick the version to pin and, for each variable that should be a pick-list, enter its allowed values. Save.

**API** — needs a user or team token; organization tokens are rejected by these endpoints:

```powershell
$h = @{ Authorization = "Bearer $env:TFC_TOKEN"; "Content-Type" = "application/vnd.api+json" }
$body = @'
{
  "data": {
    "type": "no-code-modules",
    "attributes": { "version-pin": "1.0.0", "enabled": true },
    "relationships": {
      "registry-module": { "data": { "id": "mod-xxxxxxxxxxxxxxxx", "type": "registry-module" } },
      "variable-options": { "data": [
        { "type": "variable-options",
          "attributes": { "variable-name": "vm_size", "variable-type": "string",
                          "options": ["Standard_B2s", "Standard_D2s_v3"] } }
      ] }
    }
  }
}
'@
Invoke-RestMethod -Method Post -Headers $h -Body $body "https://app.terraform.io/api/v2/organizations/<org>/no-code-modules"
```

The response's `data.id` is the **`nocode-…` ID — record it**; it is pinned in the form (§6). Later changes (moving the version pin, adding options) go to `PATCH https://app.terraform.io/api/v2/no-code-modules/<nocode-id>` with the same body shape; the ID does not change, so nothing changes in CloudBolt.

**Finding the ID of a module enabled in the UI.** The HCP Terraform UI does not display the `nocode-…` ID, and there is no endpoint that lists an organization's no-code modules (`GET /organizations/<org>/no-code-modules` returns 404; the path is POST-only). The registry module does carry a `no-code-modules` relationship (a related link, per HashiCorp's OpenAPI description of the registry-modules object), so read the module and follow it:

```powershell
$h = @{ Authorization = "Bearer $env:TFC_TOKEN" }
$module = Invoke-RestMethod -Headers $h "https://app.terraform.io/api/v2/organizations/<org>/registry-modules/private/<org>/<module-name>/azurerm"
$related = $module.data.relationships.'no-code-modules'.links.related
$nocode = Invoke-RestMethod -Headers $h ("https://app.terraform.io" + $related)
$nocode.data | Select-Object id, @{n='version_pin';e={$_.attributes.'version-pin'}}, @{n='enabled';e={$_.attributes.enabled}}
```

`<org>` appears twice because a private module's namespace is the organization name; `azurerm` is the module's provider segment. Fallback if the link is absent: open the module's page in HCP Terraform with the browser's developer tools open, filter the network calls on `no-code-modules`, and read the `nocode-…` segment of the request path. To confirm what is pinned:

```powershell
Invoke-RestMethod -Headers $h "https://app.terraform.io/api/v2/no-code-modules/<nocode-id>?include=variable_options" | ConvertTo-Json -Depth 8
```

`data.attributes.version-pin` is the version CloudBolt will deploy; `included[]` holds the variable options the order form's dropdowns read.

## 5. API token and ConnectionInfo

Docs: [API tokens](https://developer.hashicorp.com/terraform/cloud-docs/users-teams-organizations/api-tokens); permissions for provisioning: [Provision no-code infrastructure](https://developer.hashicorp.com/terraform/cloud-docs/no-code-provisioning/provisioning)

- **Team or user token only.** Enabling a module, updating it, and creating a workspace from it all reject **organization tokens**. Keep the Owners **team** token from [hcp-terraform-setup.md §6](hcp-terraform-setup.md#6-create-the-team-api-token) for the POC.
- **Permissions.** Creating a workspace from a no-code module needs, per the docs, the organization-level **Manage all projects** or **Manage all workspaces** permission, **Admin** on the project, or a custom project role that can create workspaces, write variables and apply runs. The Owners token has all of it. A least-privilege team (production follow-up) must also keep workspace **admin** on the project so the approval gate can render the attribute-level plan diff.
- **ConnectionInfo.** Create the `tf-cloud`-labeled ConnectionInfo exactly as in [hcp-terraform-setup.md §7](hcp-terraform-setup.md#7-create-the-cloudbolt-connectioninfo-label-tf-cloud) (label, host, port 443, `https`, token in the **password** field, headers empty) and re-enter the token after **every** repo sync ([§9](hcp-terraform-setup.md#9-re-enter-the-token-after-every-repo-sync)).

---

## 6. CloudBolt: pin the coordinates and author the form

**One blueprint targets one module.** Pin the coordinates as the `defaultValue` of the hidden `plugin-bdi-t474vto9.<name>` text fields in the custom form (`forms/FRM-1dxfulvq`):

| hidden field | value |
|---|---|
| `tfc_connection_info` | the `tf-cloud` ConnectionInfo global ID (`CON-…`, §5) — **FILL ME** |
| `tfc_organization` | the HCP Terraform organization name (§1) — **FILL ME** |
| `tfc_project` | the HCP Terraform project **name** (§1) — **FILL ME** |
| `tfc_nocode_module_id` | the `nocode-…` ID this blueprint deploys (§4) — **FILL ME** |

**The form is the only place these are pinned.** With a custom form attached, CloudBolt does not apply a deployment item's `parameter_defaults`, so the build item of `blueprints/BP-00meiwwz` carries none; the plugin receives exactly what the hidden form fields submit. The build plugin refuses to run while any value still contains `FILL-ME`.

**The order form** (`forms/FRM-1dxfulvq`) has two parts:

- An **Environment** dropdown (`plugin-bdi-t474vto9.env_id`) filled by the build plugin's `generate_options_for_env_id` through the `parameterOptions` endpoint — only Azure environments the ordering group may use. The chosen environment's subscription and tenant become the workspace's `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID`; the resource handler is never shown.
- A **Module Variables** Dynamic Panel. It ships authored for the sample module (`vm_name`, `resource_group_name`, `subnet_id`, `vm_size`, `admin_username`, `admin_password`, `os_image`, `tags`): resource group, subnet and image are listed from the environment, and `vm_size` lists the variable options defined on the module in HCP (§4), so define at least that one. For another module, replace the fields with one per input variable:
  - Each field's **name must equal the Terraform variable name exactly**.
  - For a variable whose allowed values you defined in §4, use a dropdown with `choicesByUrl` pointing at the Form Options webhook: `/api/v3/cmp/inboundWebHooks/form-options/run/?source=tfc_variable_options&service_item=BDI-t474vto9&variable=<name>` (`path: options`, `valueName: value`, `titleName: title`). The webhook resolves the connection and module ID server-side from the named deployment item's pins — the hidden `tfc_*` fields of this blueprint's custom form — so the query string never carries them.
  - For a variable that should come from the CloudBolt environment (resource group, subnet, size, image, location, or any env-scoped custom field), use `source=resource_group|subnet|vm_size|os_image|location|cf:<field>` with `&group={group}&env_id={plugin-bdi-t474vto9.env_id}`.
  - Mark sensitive variables' names in the hidden `_sensitive` checkbox's `choices`/`defaultValue` — they are written to the workspace `sensitive: true` and never mirrored in CloudBolt.
  - A map/object variable (e.g. `tags`) uses a `matrixdynamic` (key/value) and is written `hcl: true`.
  - Keep the naming-only `deployment_name` field (it names the CloudBolt resource and is NOT sent to Terraform).
- The form ships with `rendering_mode: jquery` and a colocated CSS file (the same layout as the VM blueprint's form). Keep both; a form imported without `rendering_mode` renders in Vue mode, where the CSS does not apply.

**Onboarding another module** = a new blueprint (cloned wiring) + a new form whose hidden fields pin the new coordinates. Zero changes to the plugins or `tfc_api`.

**Freeze the build plugin's `action_inputs` before authoring the form** — the form hardcodes the panel funnel name `plugin-bdi-<build-item-id>.parameters`; editing the plugin's inputs afterward can regenerate the field-dependency suffix and break the binding.

## 7. Restart after first sync

CloudBolt caches shared modules in-process. The build and teardown plugins import symbols from `tfc_api` (`create_no_code_workspace`, `drive_run_with_plan_approval`, `no_code_workspace_name_for_resource`, …). After the **first** sync that brings both `tfc_api` and these plugins — and after any later sync that adds a `tfc_api` symbol — **restart CloudBolt**; otherwise a plugin's import is resolved against the stale cached module and blueprint sync crashes. Re-enter the ConnectionInfo token after the sync (§5).

Inbound webhooks sync separately from blueprints: after a change to the Form Options webhook (`webhooks/IWH-yj93is5z`) or its plugin, sync `webhooks/` explicitly ([hcp-terraform-setup.md §9](hcp-terraform-setup.md#9-re-enter-the-token-after-every-repo-sync)).

## 8. Approval gate, recovery, and adopted-run attribution

The approval gate behaves exactly as the VCS blueprint's — see [hcp-terraform-setup.md §10–§11](hcp-terraform-setup.md#10-operating-the-approval-gate) for the operator mechanics (Continue Job approves; canceling rejects and discards; `cb_admin`-only resume; the API resume/cancel; the jobengine-restart recovery path, including the **Discard** button on the resource's Terraform tab). No-code specifics:

- **Subscription comes from the CloudBolt environment.** The create payload carries `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID` as `env`-category workspace variables (from the environment's Azure handler), so the auto-queued run already targets the right subscription; a retry that upserts variables writes them the same way. Workspace variables override a non-priority variable set's same-named keys.
- **Provision adopts the auto-queued run.** A no-code create makes the workspace **and** auto-queues its first run. The build plugin adopts that run into the pause; `auto_apply: false` is honored, so it waits at the confirmable gate rather than auto-applying.
- **Attribution is by stored `tfc_run_id` / resource-level, not run message.** The auto-queued run's message is TFC-authored ("Triggered via no-code provision"), so it is not parseable. The build stores the adopted run's ID on the resource (`tfc_run_id`); teardown fails fast if any **live** CloudBolt job is attached to the resource (a provision/day-2 mid-flight), else discards orphaned runs.
- **Retry** re-adopts the workspace by its deterministic `cb-nc-<global_id>` name (the name is the ownership proof — the no-code create ignores `tag-bindings`, so the `cmp:resource-id` tag is applied best-effort after create and is confirmatory only). An orphaned auto-queued run from a crashed attempt is adopted rather than dead-ending the retry.

## 9. Module-version changes (Upgrade deferred)

Every deployment runs the version pinned on the no-code module (§4). To move to a new version:

- Tag and publish the new version (§2.6), then move the pin: in the UI, open the module, **Configure Settings**, choose the newer version under **Module version** and save; or `PATCH /no-code-modules/<nocode-id>` with a new `version-pin` (§4). The `nocode-…` ID and the CloudBolt form do not change. Re-check the variable options against the new version's variables.
- **Existing deployments do not move.** HCP Terraform shows an update notice on each workspace and offers a manual upgrade (**View update** in the UI, or the API's upgrade endpoints). The CloudBolt module-version **Upgrade** day-2 action is deferred to follow-up; in the meantime, upgrade the workspace in HCP Terraform, or tear down and re-order the deployment. New orders pick up the new pin immediately.

Day-2 **Update Variables** (the shared Terraform Update action: a form built from this blueprint's order form, pre-filled with the deployment's current values) edits variable *values* only, not the module version.

---

## 10. Live end-to-end checklist

Run after the first sync + restart (§7), with the coordinates pinned in the form (§6) and the form authored for the pinned module. The blueprint's code is validated offline (metadata cross-references, script compilation, and `tfc_api` symbol resolution all pass in-repo), but the following behaviors can only be confirmed against a live instance + HCP Terraform organization. Record pass/fail + evidence.

| # | Scenario | Expected | Result |
|---|----------|----------|--------|
| 1 | Provision, approve | Workspace `cb-nc-<gid>` created from the module; job pauses at the plan gate; Continue applies; resource named; `tfc_output_*` + `tfc_var_*` populated | _pending_ |
| 2 | Provision, reject (cancel at pause) | Run discarded in TFC; resource PROVFAILED with `tfc_workspace_id` stored | _pending_ |
| 3 | Retry a PROVFAILED provision | Existing workspace adopted by name (no second no-code create); orphaned auto-queued run adopted, or a fresh run created; completes | _pending_ |
| 4 | **Vars-in-create check** | The adopted auto-queued run's plan reflects the **submitted** variables (not module defaults). If it plans against defaults, switch the build to upsert-then-fresh-run (see the note in the build plugin) | _pending_ |
| 5 | Update Variables, approve | Dialog pre-fills current values; edit + Continue applies; mirrors/outputs refreshed | _pending_ |
| 6 | Update Variables, reject | Run discarded; workspace variables reverted to snapshot; custom fields unchanged | _pending_ |
| 7 | Update Variables, unknown/sensitive key | Fail fast before any TFC write, naming the offending key | _pending_ |
| 8 | Teardown a provisioned deployment | Orphaned runs cleared; auto-confirmed destroy; workspace safe-deleted; SUCCESS | _pending_ |
| 9 | Teardown a PROVFAILED / no-workspace resource | WARNING (nothing to clean), or name-lookup fallback finds + cleans `cb-nc-<gid>` | _pending_ |
| 10 | Regression: existing **VM blueprint** (BP-b0qm83lh) provision + update + teardown | Behaves identically after the shared-module change (the engine extraction is behavior-preserving) | _pending_ |
| 11 | Best-effort tag | After provision, the workspace carries `cmp:resource-id=<gid>` (applied post-create); if absent, provisioning still succeeded (name is the ownership signal) | _pending_ |
| 12 | Environment → subscription | The workspace shows `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID` env variables equal to the chosen environment's handler, and the plan targets that subscription | _pending_ |
| 13 | Form Options webhook | With a variable option defined on the module in HCP (§4), its form dropdown lists those values; a user outside the group gets 403 from `form-options/run/` | _pending_ |
| 14 | Version pin | After moving the pin (§9), a new order provisions the new version and existing workspaces show HCP's update notice but keep running the old one | _pending_ |

Item 4 is the one residual design risk: the no-code create auto-queues a run with `auto_apply: false`, but whether it honors submitted variables (as the docs say, and unlike `tag-bindings` which it ignores) must be confirmed here. The build plugin sends vars in the create; if item 4 fails, the documented fallback is to upsert variables then drive a fresh run instead of adopting the auto-queued one.
