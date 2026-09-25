# HCP Terraform No-Code Module — Operator Setup

> The sibling runbook [hcp-terraform-setup.md](hcp-terraform-setup.md) covers
> the VCS-workspace blueprint (BP-b0qm83lh) and is the authority for shared
> mechanics (ConnectionInfo labeling, token re-entry after sync, the approval
> gate) — this doc links to it rather than restating it.

This blueprint provisions infrastructure through HCP Terraform's **no-code
provisioning** workflow: the blueprint is wired to one pinned no-code registry
module, a **static** order form (authored per blueprint) collects that module's
input variables, and a dedicated workspace is created from the module. It mirrors
the "HCP Terraform VM" blueprint (BP-b0qm83lh) — one blueprint, one target, a
static Dynamic-Panel form — differing only in the workspace-creation API path.
It is a **separate** blueprint; the two share only the `tfc_api` shared module.

---

## 1. HCP-side prerequisites

1. The module exists in the org's **private registry** and is **no-code
   enabled** (Registry → the module → enable no-code provisioning). Variable
   options defined on the module in HCP (the allowed values per variable) CAN
   drive form dropdowns live — see §3.
1. The project's credentials variable set must not be flagged *priority*, and
   its service principal needs a role in every subscription the CloudBolt
   environments point at: CloudBolt writes `ARM_SUBSCRIPTION_ID` /
   `ARM_TENANT_ID` per workspace from the chosen environment (§5).
2. You have the module's **`nocode-*` ID**. It is not shown prominently in the
   UI; get it either from the no-code provision page's network calls (devtools
   → Network → filter `no-code-modules`) or by dumping the registry modules and
   following the target module's relationships:
   ```powershell
   $h = @{ Authorization = "Bearer $env:TFC_TOKEN" }
   Invoke-RestMethod -Headers $h "https://app.terraform.io/api/v2/organizations/<org>/registry-modules" | ConvertTo-Json -Depth 15
   ```
   There is **no** GET endpoint that lists an org's no-code modules
   (`GET /organizations/:org/no-code-modules` → 404; that path is POST-only).
3. The token is a **team or user token** — **organization tokens are rejected**
   by the no-code endpoints (they cannot start runs / create configuration
   versions). See §2.

## 2. ConnectionInfo

Reuse the `tf-cloud`-labeled ConnectionInfo model exactly as the VCS blueprint
does — see [hcp-terraform-setup.md](hcp-terraform-setup.md) §7 (label, host,
port, token in the password field) and §9 (the token must be re-entered after
every repo sync, because export redacts secrets). The only no-code-specific
requirement: the token must be a **team or user token**, not an org token
(§1.3).

## 3. Per-blueprint pinning + static-form authoring

**One blueprint targets one module.** Pin the coordinates as the
`defaultValue` of the hidden `plugin-bdi-t474vto9.<name>` text fields in the
custom form (`forms/FRM-1dxfulvq`):

| hidden field | value |
|---|---|
| `tfc_connection_info` | the `tf-cloud` ConnectionInfo global_id (`CON-…`) — **FILL ME** |
| `tfc_organization` | the HCP Terraform organization — **FILL ME** |
| `tfc_project` | the HCP Terraform project **name** — **FILL ME** |
| `tfc_nocode_module_id` | the `nocode-*` module this blueprint deploys — **FILL ME** |

**The form is the only place these are pinned.** With a custom form attached,
CloudBolt does not apply a deployment item's `parameter_defaults`, so the
build item of `blueprints/BP-00meiwwz` carries none; the plugin receives
exactly what the hidden form fields submit. The build plugin refuses to run
while any value still contains `FILL-ME`.

**The order form** (`forms/FRM-1dxfulvq`) has two parts:

- An **Environment** dropdown (`plugin-bdi-t474vto9.env_id`) filled by the
  build plugin's `generate_options_for_env_id` through the `parameterOptions`
  endpoint — only Azure environments the ordering group may use. The chosen
  environment's subscription and tenant become the workspace's
  `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID`; the resource handler is never shown.
- A `Module Variables` Dynamic Panel. Replace the shipped EXAMPLE fields with
  one field per input variable of the module:
  - Each field's **name must equal the Terraform variable name exactly**.
  - For a variable whose allowed values are defined on the module in HCP,
    use a dropdown with `choicesByUrl` pointing at the Form Options webhook:
    `/api/v3/cmp/inboundWebHooks/form-options/run/?source=tfc_variable_options&service_item=BDI-t474vto9&variable=<name>`
    (`path: options`, `valueName: value`, `titleName: title`). The webhook
    resolves the connection and module ID server-side from the named
    deployment item's pins — the hidden `tfc_*` fields of its blueprint's
    custom form — so the query string never carries them.
  - For a variable that should come from the CloudBolt environment (resource
    group, subnet, size, image, location, or any env-scoped custom field),
    use `source=resource_group|subnet|vm_size|os_image|location|cf:<field>`
    with `&group={group}&env_id={plugin-bdi-t474vto9.env_id}`.
  - Mark sensitive variables' names in the hidden `_sensitive` checkbox's
    `choices`/`defaultValue` — they are written to TFC `sensitive: true` and
    never mirrored in CloudBolt.
  - A map/object variable (e.g. `tags`) uses a `matrixdynamic` (key/value)
    and is written `hcl: true`.
  - Keep the naming-only `deployment_name` field (it names the CloudBolt
    resource and is NOT sent to Terraform).
- To fetch the module's variable list for authoring:
  ```powershell
  $h = @{ Authorization = "Bearer $env:TFC_TOKEN" }
  Invoke-RestMethod -Headers $h "https://app.terraform.io/api/v2/no-code-modules/<nocode-id>?include=variable_options" | ConvertTo-Json -Depth 8
  ```
  (returns the module's variable options; the per-version variable list is
  the `module-variables` relationship of the registry module version).

**Onboarding another module** = a new blueprint (cloned wiring) + a new form
whose hidden fields pin the new coordinates. Zero changes to the plugins or
`tfc_api`.

**Freeze the build plugin's `action_inputs` before authoring the form** — the
form hardcodes the panel funnel name `plugin-bdi-<build-item-id>.parameters`;
editing the plugin's inputs afterward can regenerate the field-dependency suffix
and break the binding.

## 4. Restart after first sync

CloudBolt caches shared modules in-process. The new plugins import symbols added
to `tfc_api` in this change (`create_no_code_workspace`,
`drive_run_with_plan_approval`, `no_code_workspace_name_for_resource`, …). After
the **first** sync that brings both the updated `tfc_api` and these plugins,
**restart CloudBolt** — otherwise a plugin's import of a newly added symbol is
resolved against the stale cached module and crashes blueprint sync. Re-enter
the ConnectionInfo token after the sync (§2).

## 5. Approval gate, recovery, and adopted-run attribution

The approval gate behaves exactly as the VCS blueprint's — see
[hcp-terraform-setup.md](hcp-terraform-setup.md) §10–§11 for the operator
mechanics (Continue Job approves; canceling rejects and discards; cb_admin-only
resume; the jobengine-restart recovery path). No-code specifics:

- **Subscription comes from the CloudBolt environment.** The create payload
  carries `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID` as `env`-category workspace
  variables (from the environment's Azure handler), so the auto-queued run
  already targets the right subscription; a retry that upserts variables
  writes them the same way. Workspace variables override a non-priority
  variable set's same-named keys.
- **Provision adopts the auto-queued run.** A no-code create makes the workspace
  **and** auto-queues its first run. The build plugin adopts that run into
  the pause; `auto_apply: false` is honored, so it waits at the confirmable gate
  rather than auto-applying.
- **Attribution is by stored `tfc_run_id` / resource-level, not run message.**
  The auto-queued run's message is TFC-authored (`"Triggered via no-code
  provision"`), so it is not parseable. The build stores the adopted run's ID on
  the resource (`tfc_run_id`); teardown fails fast if any **live** CloudBolt job
  is attached to the resource (a provision/day-2 mid-flight), else discards
  orphaned runs.
- **Retry** re-adopts the workspace by its deterministic `cb-nc-<global_id>`
  name (the name is the ownership proof — the no-code create ignores
  `tag-bindings`, so the `cmp:resource-id` tag is applied best-effort after
  create and is confirmatory only). An orphaned auto-queued run from a crashed
  attempt is adopted rather than dead-ending the retry.

## 6. Module-version changes (Upgrade deferred)

The no-code **module-version Upgrade** day-2 action is **deferred to
follow-up**. To move a deployment to a new module version in the meantime:

- Re-pin the module's version in HCP (the no-code create uses the module's
  configured version pin), or
- Teardown + re-order the deployment.

Day-2 **Update Variables** (the shipped action) edits variable *values* only, not
the module version.

---

## 7. Live end-to-end checklist

Run after the first sync + restart (§4), with the coordinates pinned in the
form (§3) and
the form authored for the pinned module. The blueprint's code is validated
offline (metadata cross-references, script compilation, and `tfc_api` symbol
resolution all pass in-repo), but the following behaviors can only be confirmed
against a live instance + TFC org. Record pass/fail + evidence.

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
| 12 | Environment → subscription | The workspace shows `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID` env variables equal to the chosen environment's handler, and the plan targets that subscription | _pending_ |
| 13 | Form Options webhook | With a module variable option defined in HCP, its form dropdown lists those values; a user outside the group gets 403 from `form-options/run/` | _pending_ |
| 11 | Best-effort tag | After provision, the workspace carries `cmp:resource-id=<gid>` (applied post-create); if absent, provisioning still succeeded (name is the ownership signal) | _pending_ |

Item 4 is the one residual design risk: the no-code create auto-queues a run
with `auto_apply: false`, but whether it honors submitted variables (as the
docs say, and unlike `tag-bindings` which it ignores) must be confirmed here. The build plugin sends vars
in the create; if item 4 fails, the documented fallback is to upsert variables
then drive a fresh run instead of adopting the auto-queued one.
