# HCP Terraform Setup Runbook

One-time manual setup for the **HCP Terraform workspace-per-deployment integration** (the "HCP Terraform VM" blueprint, its day-2 actions, and the `tfc_api` shared module). Follow this document top to bottom on a fresh HCP Terraform (TFC) account and a fresh CloudBolt instance synced from this repo; no other source is required.

What this runbook does **not** cover: authoring the Terraform configuration repo itself. It is assumed to exist with a stable variable schema (a `variables.tf` the blueprint inputs mirror). It also does not cover ordering the blueprint — that is the end-to-end verification pass, not setup.

CloudBolt never creates TFC organizations, projects, variable sets, or VCS connections — those are manual, and this is the manual. CloudBolt **does** create one TFC workspace per deployment automatically (named `cb-vm-<resource global ID>`, tagged `cmp:resource-id`); you never pre-create or hand-edit workspaces.

## Prerequisites checklist

Before starting, confirm you have:

- [ ] The ability to create a **brand-new** HCP Terraform organization at [app.terraform.io](https://app.terraform.io) (any user can; the free tier suffices for this POC).
- [ ] A **sandbox-only Azure subscription** and a service principal in it (client ID, client secret, tenant ID, subscription ID). Nothing production-adjacent — see [§1](#1-read-this-first-the-poc-trust-model) for why this is a control, not a convenience.
- [ ] The Terraform configuration repo ("template repo") on a VCS host TFC supports, plus **admin rights on that repo** so you can restrict who has write access.
- [ ] A VCS account able to authorize an OAuth connection from TFC to that repo (a personal account is acceptable for the POC; a dedicated service-account identity is a named follow-up).
- [ ] CloudBolt administrator (`cb_admin`) access on the target instance, with this repo already synced via Source Control Repos.
- [ ] At least one CloudBolt **Environment** on an Azure resource handler for the sandbox subscription, entitled to the ordering group, with resource groups and subnets imported, VM sizes enabled and an OS build available (section 8c).

## 1. Read this first: the POC trust model

Two facts define every security decision below. Do not skip this section.

**Fact 1 — the TFC token is org-root, and any CloudBolt plugin author can read it.** The POC authenticates to TFC with an **Owners team token**, which carries full organization-level permissions: every project, every workspace, every organization setting. That token is stored in a CloudBolt ConnectionInfo `password` field, and any user who can author or edit a CloudBolt plugin can read any ConnectionInfo password from plugin code. The effective trust boundary is therefore:

> **CloudBolt plugin-author ≈ TFC organization owner.**

This is why a **dedicated sandbox TFC organization containing only this POC's project is a hard prerequisite** — the organization must contain nothing else worth protecting, because everyone who can write a plugin on this CloudBolt instance effectively owns it. A least-privilege custom team token is the production follow-up (requires a paid TFC tier), not part of this POC.

**Fact 2 — write access to the template repo is equivalent to access to the Azure credentials.** `terraform plan` executes provider and data-source code with the variable set's `ARM_*` credentials present in the process environment. Anyone who can merge to the template repo's tracked branch can exfiltrate the service-principal secret during the plan phase of the *next run on any workspace* — and nothing suspicious need appear in the plan diff. The CloudBolt approval gate protects **Azure state** (no apply without human review); it does **not** protect the **credentials**, which are exposed at plan time, before any approval. Auto-confirmed destroy runs (teardown) get no plan review at all, so a merged template change also rides along unreviewed with the next delete.

The POC controls, restated as a table:

| Control | What it actually protects against |
|---|---|
| Dedicated sandbox TFC org, only this project in it | Owners-token blast radius: plugin authors can do anything in the org, so the org contains nothing else |
| Sandbox-only Azure subscription behind the variable set | Credential exfiltration via template-repo write access costs you nothing real |
| Restricted write access + branch protection on the template repo | Raises the bar for planting credential-exfiltrating or destructive template changes |
| Plan approval gate (pause before apply) | Unreviewed *changes to Azure state* — not credential exposure |

## 2. Create the sandbox organization

Docs: [Organizations](https://developer.hashicorp.com/terraform/cloud-docs/users-teams-organizations/organizations)

1. Sign in at [app.terraform.io](https://app.terraform.io) and create a **new** organization (e.g. `acme-cloudbolt-poc`). Never reuse an existing organization, no matter how convenient.
2. Record the organization name; it is pinned on the blueprint as `tfc_organization` (section 8b).
3. Keep this organization permanently single-purpose: **only** this POC's project, ever. If someone later wants to add an unrelated project, that is the signal to revisit the token model first (least-privilege team token, paid tier).

The free tier works: the POC uses only the built-in **Owners** team. Custom teams with scoped permissions (the production-hardening path) require a paid tier.

## 3. Create the project

Docs: [Manage projects](https://developer.hashicorp.com/terraform/cloud-docs/projects/manage); API reference: [Projects API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/projects)

1. In the new organization, create one project. Suggested name: **`cloudbolt-vm-deployments`**.
2. Record the project name; it is pinned on the blueprint as `tfc_project` (section 8b). The build plugin resolves the project ID by name at runtime ([Projects API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/projects)).
3. Create **no workspaces**. CloudBolt creates one workspace per deployment via the [Workspaces API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces), named `cb-vm-<resource global ID>`, tagged `cmp:resource-id` (see [workspace tags](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/tags)), with VCS-triggered and speculative runs disabled — every run is API-driven by CloudBolt. Do not hand-create workspaces in this project, and do not hand-edit the settings or variables of the ones CloudBolt creates; the POC does not reconcile manual TFC-side edits.

## 4. Create the project-scoped variable set (Azure credentials)

Docs: [Variables in HCP Terraform](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/variables) (variable sets, sensitive values); API reference: [Workspace variables](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspace-variables)

The Azure credentials live **only** here — in TFC, never in CloudBolt, never in this repo. CloudBolt holds exactly one secret for this integration: the TFC team token (§6/§7).

1. In **Organization Settings → Variable sets**, create a variable set (e.g. `azure-sandbox-credentials`).
2. Scope it to the **`cloudbolt-vm-deployments` project only**. Do not make it global — workspaces in the project inherit it automatically, and nothing else should.
3. Add four variables, every one with category **Environment** and the **Sensitive** flag checked:

   | Key | Value | Category | Sensitive |
   |---|---|---|---|
   | `ARM_CLIENT_ID` | sandbox service-principal application (client) ID | Environment | yes |
   | `ARM_CLIENT_SECRET` | sandbox service-principal secret | Environment | yes |
   | `ARM_TENANT_ID` | sandbox tenant ID | Environment | yes |
   | `ARM_SUBSCRIPTION_ID` | **sandbox** subscription ID | Environment | no |

   `ARM_SUBSCRIPTION_ID` is an identifier, not a secret; leaving it non-sensitive keeps it readable for troubleshooting. Do **not** flag the variable set as *priority*: CloudBolt writes `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID` on each workspace from the chosen CloudBolt environment (section 8c), and a priority set would override them. The service principal needs a role in every subscription those environments point at.

4. The backing subscription must be **sandbox-only** — disposable resource groups, no production data, no peering or trust toward anything that matters.

**Why sandbox-only, again:** marking a variable Sensitive makes it write-only in the TFC UI and API, but it is still injected into the environment of every plan and apply. Anyone with template-repo write access can read these values during the plan phase of the next run (Fact 2 in §1). The approval gate does not protect them. Treat the credential set as already shared with everyone who can merge to the template repo, and size the subscription's blast radius accordingly.

## 5. Connect the VCS provider and lock down the template repo

Docs: [Connect to VCS Providers](https://developer.hashicorp.com/terraform/cloud-docs/vcs); API references the shared module uses at runtime: [OAuth clients](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/oauth-clients), [OAuth tokens](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/oauth-tokens)

1. In **Organization Settings → Version Control → Providers**, add the provider hosting the template repo (GitHub, GitLab, etc.) and complete the OAuth authorization flow. A personal VCS account is acceptable for the POC; a dedicated service account is a named follow-up.
2. Grant the connection access to the template repo (if your VCS host supports per-repository app installs, grant only that repo).
3. Record the repo identifier (`owner/repo`) and the branch the workspaces should track; both are pinned on the blueprint's build item (section 8b). CloudBolt attaches the repo to every workspace it creates through this connection, resolving the connection's OAuth token at runtime by listing the organization's OAuth clients ([OAuth clients API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/oauth-clients)). **Keep exactly one VCS provider configured in the organization**; a second one makes that resolution ambiguous.
   CloudBolt attaches the selected repo to every workspace it creates through this connection, resolving the connection's OAuth token at runtime by listing the organization's OAuth clients. **Keep exactly one VCS provider configured in the organization** — a second one makes that resolution ambiguous, and the POC needs only the one template repo.
4. **Restrict write access on the template repo — this is a credential-protection control, not housekeeping** (Fact 2 in §1):
   - Collaborator list cut to the minimum set of template maintainers.
   - Branch protection on the tracked branch: no direct pushes, required pull-request review.
   - Remember that a merged change rides along with the **next run of every deployment workspace** — plan review catches it for build and day-2 runs, but teardown's destroy runs are auto-confirmed and get no review. Pinning workspaces to a tag/release instead of a branch is a named follow-up.

## 6. Create the team API token

Docs: [Manage API tokens — team API tokens](https://developer.hashicorp.com/terraform/cloud-docs/users-teams-organizations/api-tokens)

The POC uses the built-in **Owners** team, which already holds organization-level permissions including workspace creation in every project — no permission assignment is needed (that is exactly the blast-radius problem §1 exists to contain).

1. In **Organization Settings → Teams → Owners**, generate a **team API token**. If your TFC plan offers token expiration, set one and calendar the rotation.
2. The token is displayed **once**. Paste it directly into the CloudBolt ConnectionInfo (§7) and store it nowhere else — not in this repo, not in a wiki, not in a chat message.
3. Regenerating the team token invalidates the old one; if you rotate it, update the ConnectionInfo in the same sitting or every TFC-backed job on the instance starts failing with 401s.

Production follow-up (not now): replace the Owners token with a least-privilege custom team scoped to the one project (paid tier required).

## 7. Create the CloudBolt ConnectionInfo (label: `tf-cloud`)

In the CloudBolt UI, create a Connection Info record (*Admin → Connection Info*). The shared module selects connections by **label, not name** — the name is free-form; what matters is the `tf-cloud` label:

| Field | Value |
|---|---|
| Name | anything descriptive, e.g. `HCP Terraform (sandbox)` |
| **Labels** | **`tf-cloud`** — required; this is how the integration finds the connection |
| Protocol | `https` |
| IP/Hostname | `app.terraform.io` (or your Terraform Enterprise host) |
| Port | `443` |
| Password | the team API token from §6 |

Leave the username blank (TFC bearer-token auth ignores it) and leave the **headers field empty**. The `password` field is the only masked, vault-resolvable ConnectionInfo field; the headers field renders unmasked in the UI. The shared module builds the `Authorization: Bearer <token>` header at runtime from the password field — putting the token anywhere else both breaks the lookup and displays it in cleartext.

**Multiple connections are supported:** label each `tf-cloud`; a blueprint pins the one it uses via `tfc_connection_info` in its `parameter_defaults` (section 8b), and the build plugin stores that global ID on the resource so day-2 and teardown reuse it. The label is enforced at run time; removing it takes the connection out of service.

If TFC requests start failing with 401/403, the plugin error message names the selected ConnectionInfo. Check, in order: the token was redacted by a repo sync (§9), the token was regenerated in TFC (§6.3), the Owners team membership changed.

## 8. Configure the integration (config-as-code)

Edit configuration **in this repo**, commit, and let the instance sync. The next sync overwrites instance-side edits.

Where each value lives:

| Value | Where | Who sets it |
|---|---|---|
| ConnectionInfo, organization, project, repo, branch, working directory | `parameter_defaults` on the build deployment item in `blueprints/BP-b0qm83lh/BP-b0qm83lh_metadata.json` | operator, once per blueprint |
| Environment (subscription, tenant) | chosen on the order form; derived from the environment's Azure resource handler | orderer |
| Template variables | the order form's variables panel | orderer |
| State outputs | none: every output in the applied state is recorded as `tfc_output_<name>` | - |

### 8a. Account-level: nothing to edit

`tfc_api` has no instance-specific values. The CloudBolt URL recorded on each workspace (its "source" link back to CloudBolt) is taken at run time from the portal the order was placed on, falling back to the default portal. Set the portal's site URL under *Admin > Portals* if the link should differ from the domain users log in with.

### 8b. Per-blueprint: pinned TFC coordinates

Edit the `value` of each `parameter_defaults` entry on the build item (names end in `_a<n>`; the suffix is rewritten on import):

| Name | Set it to |
|---|---|
| `tfc_connection_info` | the `tf-cloud` ConnectionInfo's global ID (`CON-...`, section 7) |
| `tfc_organization` | the organization name (section 2) |
| `tfc_project` | the project name (section 3) |
| `tfc_repo_identifier` | `owner/repo` of the template repo (section 5) |
| `tfc_branch` | the tracked branch (ships `main`) |
| `tfc_working_directory` | subdirectory holding the configuration; add the entry only if not the repo root |

The build plugin refuses to run while any value contains `FILL-ME`.

**Edit the same values in the form too.** When a custom form is attached, CloudBolt does not apply the deployment item's `parameter_defaults`, so `forms/FRM-t3v8zpb7` carries each coordinate as a hidden `plugin-bdi-lwys1ug9.<name>` text field with a `defaultValue`. Keep the two copies identical; the form copy is the one the plugin actually receives.

### 8c. The environment and the order form

The orderer picks a **CloudBolt Environment**; nothing else about Azure is typed by hand:

- The environment's Azure resource handler supplies the subscription and tenant. The build plugin writes them to the workspace as `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID` environment variables, which override the same keys inherited from the project's variable set (section 4). The handler is never shown to the user; the dropdown is built from `group.get_available_environments()`.
- Resource group, subnet, VM size and OS image dropdowns are filled from that environment by the **Form Options** inbound webhook (`webhooks/IWH-yj93is5z`, `GET /api/v3/cmp/inboundWebHooks/form-options/run/?source=...&group=...&env_id=...`). It uses the orderer's session, checks group membership and environment entitlement, and reads resource groups (`resource_group_arm` env options, falling back to a live subscription listing), subnets (imported on the environment, returned as full ARM IDs), sizes (`node_size` env options) and images (OS builds available on the environment, as `publisher:offer:sku:version`).
- The template's variables live in a Dynamic Panel named `plugin-bdi-lwys1ug9.parameters` in `forms/FRM-t3v8zpb7`; each element name is the exact Terraform variable name. The panel is funneled into the build plugin's single `parameters` input. `vm_name` is inside the panel. The hidden `_sensitive` checkbox lists variables written to HCP as sensitive (ships `["admin_password"]`).

The environment must have what the dropdowns read: resource groups and subnets imported, sizes enabled, at least one OS build (*Admin > Environments > the env > Parameters / Networks / OS Builds*).

**Onboarding another Terraform template:** copy `BP-b0qm83lh` with fresh IDs, pin its `parameter_defaults`, and author a form whose panel elements match the new `variables.tf`. Reuse the webhook for any environment-derived field (`source=resource_group|subnet|vm_size|os_image|location|cf:<field>`). No plugin or shared-module change. Freeze the build plugin's `action_inputs` before authoring: the form hardcodes the `plugin-bdi-<id>` names.

The template repo's `variables.tf` must match the form (`vm_name`, `resource_group_name`, `subnet_id`, `vm_size`, `admin_username`, `admin_password`, `os_image`, `tags`). The azurerm provider reads `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID` itself, so the template declares no subscription variable; derive `location` from the subnet's VNet.

## 9. Re-enter the token after every repo sync

Per this repo's round-trip rules (see `AGENTS.md`, "Round-trip is not lossless"): when CloudBolt exports content, secrets are redacted to placeholder strings (`"YOUR_CREDENTIALS"`, `"YOUR_AUTH_INFO"`, …) and the importer skips those placeholders on re-sync. Secrets never travel through the repo — by design.

The operational consequence: **after every sync of this repo into the instance, the team token must be re-entered in every `tf-cloud`-labeled ConnectionInfo.** Make "open each labeled ConnectionInfo and re-enter its token" a standing post-sync step. The failure signature when this is missed is every TFC-backed job failing fast with a 401 error that names the ConnectionInfo. (Labels themselves survive syncs — only secrets are redacted.)

## 10. Operating the approval gate

Provision and day-2 jobs pause after `terraform plan` completes and wait for a human decision before anything is applied. (Teardown destroy runs do **not** pause — deletion is already an explicit, confirmed user action.) Run lifecycle background: [Run states](https://developer.hashicorp.com/terraform/cloud-docs/run/states), [Runs API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run).

**Where:** the **job detail page** of the paused job (reachable from the order, the resource's job history, or the Jobs list — status `PAUSED`). The job output shows, in order: the plan's resource **add/change/destroy counts**; a **⚠ Terraform warnings block** whenever the plan log contains `Warning:` lines (see below — read this before approving); an excerpt of the plan log (tail-preserved, so Terraform's change summary survives truncation); and a **deep link to the run in TFC** ([Plans API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/plans) is where those counts come from).

**Read the warnings block before approving.** Because variable validation is delegated to Terraform, a **misspelled variable name** is not rejected up front — Terraform treats the unknown key as a `Warning: Value for undeclared variable` and, if the real variable it was meant to set has a default in the template, the plan **succeeds on that stale default** and the counts look normal. The warnings block surfaces those lines (it is built from the full log and never truncated) so the approver can catch the typo before applying. (A *required* variable left with no value is the loud case instead: Terraform errors the plan and the job fails with the run URL — before any approval pause.)

**Who:** only a CloudBolt administrator (`cb_admin`) can resume a paused job. The requester cannot approve their own plan. Practically: every provision and every day-2 change needs a `cb_admin` in the loop. (Delegated approval is a named follow-up.)

| Decision | How | What happens |
|---|---|---|
| Approve | Click **Continue Job** on the job detail page | The plugin confirms the apply in TFC; the job polls to completion and records outputs |
| Reject | **Cancel** the job | The plugin discards the TFC run. Provision: the resource ends `PROVFAILED`, still carrying its workspace ID — delete it to clean up. Day-2: workspace variables are reverted to pre-action values |
| Do nothing | — | The global job timeout cancels the job through the same reject path (run discarded) |

**Programmatic approval (API).** The same `cb_admin`-gated resume/cancel is available through CloudBolt's v3 jobs API, so a pipeline or agent can drive the gate without the UI:

- **Approve:** `POST /api/v3/jobs/<job_id>/resume/` with a `cb_admin` bearer token (equivalent to clicking *Continue Job*).
- **Reject:** `POST /api/v3/jobs/<job_id>/cancel/` (equivalent to *Cancel*, takes the run through the same reject path).

The RBAC check is identical to the UI — a non-`cb_admin` token is refused — so the full lifecycle (order → approve plan → run day-2 → delete) is automatable end to end. Find the paused job's ID from the order, the resource's job history, or `GET /api/v3/jobs/?status=PAUSED`.

**The approval window is the global `job_timeout` GlobalPreference — default 8 hours.** This is a platform-wide setting: raising it lengthens **every** job's timeout on the instance, not just Terraform approvals. Decide deliberately with your CloudBolt administrator before touching it.

Two operational notes:

- Each paused job pins one job-engine worker thread for the whole wait. Fine at POC volumes; it is the binding constraint before production scale.
- Anyone with apply permission on the workspace **in TFC** can confirm a run CloudBolt is holding for review. CloudBolt detects this on resume, proceeds, and logs it as *approved out-of-band* — the gate is only as strong as TFC workspace access control, which is why end users get no TFC accounts and the Owners team stays admins-only.

### 10a. Day-2 changes: Terraform Update and Resize

A deployed resource has two day-2 actions, both running through the same plan-approval gate above:

- **Terraform Update** (generic) opens a dialog with a single **Parameters (JSON)** field, pre-filled with the deployment's current variable values as a JSON object. Edit the values and submit. Rules the operator should know:
  - The field must be a **JSON object** of `variable name → value` (a pasted array or scalar is rejected before any TFC call).
  - You may only edit variables the deployment already manages. A key the deployment does not know (e.g. a typo, or a variable added to the template later) is **rejected with a message naming the known set** — it is not written. (There is no "add a new variable to an existing deployment" path; that requires re-provisioning under an updated template — see the shared-branch note in the plan's risks.)
  - **Removing a key is a no-op** — its current value rides along unchanged (there is no delete-variable path in TFC). **Leaving a value blank** unsets it for that run.
  - Do **not** paste secrets into this field — it is group-visible job output (§12).
- **Resize** is the guided shortcut for the one common change: it presents `vm_size` as a validated dropdown with the current size pre-selected. `vm_size` is also editable through the generic Terraform Update, but Resize is the friendlier path for it.

Both actions snapshot the current variables first and, on reject, revert the workspace to that snapshot; the resource's custom-field mirrors are only updated on a successful apply.

## 11. Recovery: paused job killed by a jobengine restart

This is the integration's known fragile edge. If the jobengine restarts while a job is paused at the approval gate, the paused frame dies: **the job is marked `CANCELED`, no plugin cleanup runs**, and the unconfirmed TFC run is orphaned — it sits in *needs confirmation* and keeps the workspace busy, blocking every future run on it.

**Symptoms:**

- A job that was `PAUSED` shows `CANCELED` after a jobengine restart, with no reject/cleanup messages in its output.
- The deployment's workspace in TFC shows a current run waiting for confirmation that nobody is waiting on.
- New day-2 actions on that deployment **fail fast** with a "change already pending" message.

That fail-fast is **intentional**: the day-2 concurrency guard blocks new actions until the orphaned run is gone, because two pending change sets on one workspace cannot be reasoned about. **Re-running the day-2 action is NOT the recovery path** — it will keep failing fast until you clear the orphan.

**Recovery — either path works:**

1. **Discard the orphaned run in the TFC UI.** Open the run (use the run URL from the dead job's output, or the workspace's *Runs* page) and click **Discard run**. The workspace unblocks; day-2 actions work again.
2. **Delete the resource in CloudBolt.** Teardown is the designated recovery path for orphaned runs: it discards non-final runs whose owning CloudBolt job is no longer running before creating its destroy run. Use this when the deployment is expendable anyway.

One after-effect to know about: if the killed job was a day-2 action that had already written its new variable values to the workspace, those values stay there (the cleanup that would revert them never ran). The next **successful** day-2 run self-heals this — every run rewrites the full variable set from CloudBolt's records overlaid with the dialog values — so no manual TFC-side variable editing is needed or wanted.

## 11a. Migration assumption: no pre-refactor resources

This integration assumes **no deployments were provisioned before the blueprint-agnostic refactor landed.** Post-refactor, every day-2 and teardown action reads `tfc_organization` (and `tfc_variable_names`) from the resource and passes the org to the TFC client; a resource created under the older code never had those custom-field values, so its **first Update, Resize, or teardown would fail fast** with a `TFCConfigError` about a missing organization — it could not even be decommissioned cleanly.

For this POC that set is empty (the integration had not shipped to real deployments), so no backfill is built. **If any pre-refactor resource does exist**, stamp it once before running any day-2/teardown action: set `tfc_organization` to the organization its workspace lives in, and set `tfc_variable_names` to the comma-separated names of its existing `tfc_var_*` custom fields. A one-time recurring job over the blueprint's resources is the clean way to do it at scale.

A softer edge of the same kind: resources provisioned before **connection selection** landed have no `tfc_connection_info` value. That is handled without backfill — when the ref is blank, `get_client` resolves the connection unambiguously **as long as exactly one** `tf-cloud`-labeled ConnectionInfo exists. If you have legacy resources AND want a second labeled connection, stamp `tfc_connection_info` (the ConnectionInfo global ID) on the legacy resources first.

## 12. Keep secrets out of plan output

The plan-log excerpt written to job output at the approval gate is **group-visible**: any CloudBolt user with view rights on the job or resource in the group can read it — not just the approving `cb_admin`. The same goes for the `tfc_output_*` custom fields recorded on the resource ([sensitive-marked outputs](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/state-version-outputs) are nulled by TFC and never recorded, but unmarked ones land in plaintext).

The control is **template hygiene**, owned by the template repo maintainers:

- The template must not echo sensitive values in plan output — no secrets interpolated into resource arguments that show in diffs, no `local-exec`/`external` blocks printing credentials, sensitive variables and outputs marked `sensitive = true`.
- Treat "outputs are non-sensitive **or marked `sensitive = true`**" as a hard template-repo convention, and it carries more weight now: outputs are **auto-discovered** — every output in the applied state is recorded to a plaintext `tfc_output_*` custom field (there is no allowlist to keep a new output out). The one backstop is TFC itself: outputs marked `sensitive = true` come back `null` on the [state-version-outputs API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/state-version-outputs) and are therefore never recorded. **Marking secret-bearing outputs `sensitive = true` is mandatory, not advisory.**
- CloudBolt strips lines matching known secret patterns (the `ARM_*` names, values flagged sensitive) from the excerpt as defense-in-depth — do not rely on it.

## 13. Verify the setup

Quick smoke checklist before handing the instance over (the full lifecycle pass — provision, update, resize, teardown, reject path — is the integration's acceptance test, driven from this repo's plan, not this runbook):

- [ ] The TFC organization contains exactly one project (`cloudbolt-vm-deployments`) and no workspaces yet.
- [ ] The variable set is scoped to that project, is **not** flagged priority, and shows four **Environment**-category variables (`ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_TENANT_ID` sensitive; `ARM_SUBSCRIPTION_ID` may be plain).
- [ ] The VCS provider shows a healthy OAuth connection; the template repo's tracked branch has branch protection and a minimal collaborator list.
- [ ] The Owners team API token exists; the value lives only in the CloudBolt ConnectionInfo.
- [ ] At least one ConnectionInfo carries the **`tf-cloud` label** and reads `https` / `app.terraform.io` (or the TFE host) / `443`, password set, headers empty.
- [ ] The build-item `parameter_defaults` in `blueprints/BP-b0qm83lh/BP-b0qm83lh_metadata.json` pin connection, organization, project, repo and branch.
- [ ] Ordering `BP-b0qm83lh` renders the **custom form**: group, an **Environment** dropdown listing only Azure environments the group may use, and a variables panel whose Resource Group / Subnet / VM Size / OS Image dropdowns fill once an environment is chosen (browser network tab: `form-options/run/` returns 200 with `options`).
- [ ] After approval, the workspace in HCP shows `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID` as workspace environment variables matching the chosen environment's subscription.
- [ ] After a first successful provision, the resource shows a `tfc_output_<name>` field for **every** output in the template's `outputs.tf` (auto-discovered — no output list is configured anywhere).
- [ ] A `cb_admin` knows they own approvals (§10), reads the **⚠ Terraform warnings block** before approving (§10 — it is the only signal for a mistyped variable name), and knows where to find a paused job.
- [ ] Whoever runs the jobengine knows the restart-recovery drill (§11).
