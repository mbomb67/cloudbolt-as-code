# Azure Bicep Deployment Engine — Setup Runbook

Stand up the Azure side, the GitHub connection, and the CloudBolt configuration
required by the Bicep deployment engine (the `github` and `bicep_engine` shared
modules, the generic build/teardown/day-2 plugins, and the `scaffold-bicep`
skill). A fresh operator should be able to follow this end to end with no other
source. POC posture: a sandbox Azure subscription, GitHub only, resource-group
and subscription scopes (see §4a).

## 1. GitHub connection

1. Create a **fine-grained, read-only Personal Access Token** scoped to just the
   template repositories (`Contents: read`). Prefer a short expiry (e.g. 90
   days) and a documented rotation procedure; a GitHub App (narrow, short-lived
   tokens) is the better production option and is already supported by the
   `github` shared module.
2. In CloudBolt, create a **ConnectionInfo named exactly `GitHub`** (the name
   the engine resolves by). Put the PAT in the **password** field. Protocol
   `https`, host `api.github.com`, port `443`.
3. **Secrets-redaction caveat:** on every repo sync, CloudBolt redacts
   ConnectionInfo secrets to a placeholder and the importer skips them — so the
   GitHub token must be **re-entered after every sync**. This is expected; it is
   not a failure.

## 2. Azure subscription, service principal, and RBAC

1. Use a **dedicated sandbox Azure subscription** for the POC — the engine
   deploys and deletes real resources.
2. The Azure resource handler in CloudBolt is backed by a service principal.
   Grant it, scoped to the sandbox subscription (or the target resource group):
   - `Microsoft.Resources/deploymentStacks/*` (create/read/update/delete stacks)
   - permission to create/modify/delete the resource types the templates deploy
     (typically **Contributor** on the resource group is sufficient for the POC)
   - `Microsoft.Resources/deployments/*` (what-if runs on the deployments API)
3. **Subscription-scoped templates** (ones that create resource groups, such
   as the `Azure Resource Group - Bicep` blueprint) deploy the stack at the
   subscription, so the service principal additionally needs, at subscription
   scope: `Microsoft.Resources/subscriptions/resourceGroups/write` and
   `/delete`, `Microsoft.Resources/deploymentStacks/*`, and
   `Microsoft.Resources/deployments/*` (what-if). **Contributor on the
   subscription** covers all of this for the POC.
4. The engine reads the subscription id from the handler's `serviceaccount`
   field and the tenant from `azure_tenant_id` (the verified house pattern). No
   Azure credentials are stored in this repo or in plugin metadata.

## 3. Appliance prerequisites (compilation)

The engine self-bootstraps the Bicep binary on first use — **no manual install**
— but the appliance must allow it:

1. **Binary execution must be permitted.** The cache dir lives under
   `settings.PROSERV_DIR`. Verify a jobengine worker can `exec` a downloaded,
   `chmod +x`'d file from there: the dir must NOT be on a `noexec` mount and
   must not be blocked by SELinux. **If exec is blocked, every deployment fails
   at the compile step** — confirm this on a representative appliance before
   relying on the engine. (Test: as the CloudBolt service account, download the
   pinned binary to `<PROSERV_DIR>/bicep/<version>/bicep`, `chmod +x`, run
   `bicep --version`.)
2. **Egress.** The appliance needs outbound HTTPS to:
   - the Bicep release host (GitHub releases by default; set a mirror URL in the
     config block for restricted networks)
   - `api.github.com` (template archive fetch)
   - `management.azure.com` and `login.microsoftonline.com` (ARM + token)
   - the module registry (`mcr.microsoft.com` / a private ACR) **only if**
     templates use registry (`br:`) modules.
3. **Disk.** Each paused deployment holds an extracted template checkout for the
   length of the approval window (up to the global job timeout, default 8h).
   Size disk for one checkout per concurrently-paused order.

## 4. Engine config block

Edit the config block at the top of
`shared_modules/SHM-bbswv27r/SHM-bbswv27r_script.py`:

- `BICEP_VERSION` — the pinned Bicep CLI version.
- `BICEP_SHA256` — **independently verify** this against Microsoft's published
  release checksum. Do not trust a checksum that arrived in the same change as a
  `BICEP_DOWNLOAD_URL_TEMPLATE` edit. A PR that touches both the URL and the
  checksum is security-sensitive and needs explicit sign-off — the checksum only
  protects integrity relative to its pinned value.
- `BICEP_DOWNLOAD_URL_TEMPLATE` — release URL or an internal mirror.
- `OUTPUT_ALLOWLIST` — `None` captures all non-secure outputs; set a list to
  restrict which template outputs become custom fields.
- `RESOURCE_NAME_OUTPUT_CANDIDATES` — output name(s) used to set `resource.name`.
- `RESOURCE_NAME_PARAM_CANDIDATES` — fallback when the template has no such
  output: the first matching non-secure string *parameter* (e.g. `rgName`)
  names the resource instead.
- `DEFAULT_STACK_LOCATION` / `STACK_LOCATION_PARAM_CANDIDATES` — the
  deployment-metadata location a subscription-scoped stack/what-if must carry
  (ARM requires it; RG-scoped stacks inherit the RG's). The plugins use the
  value of the first listed template parameter that is set (e.g. `rgLocation`),
  else the default.
- `AZURE_RETRY_INTERVAL_S` / `AZURE_RETRY_WINDOW_S` — every Azure HTTP call
  (token, ARM, what-if, stack polls) retries transient failures (connection
  errors such as "Network is unreachable", timeouts, HTTP 429/5xx) every
  10 s for up to 60 s before failing with a clean "could not reach
  management.azure.com" message. 4xx responses are never retried.
- `DENY_SETTINGS_MODE` — `none` for the POC (see §6).

The pinned version must match what `scaffold-bicep` uses locally so the schema
the scaffold curates matches what the appliance compiles.

## 4a. Template scopes — one build plugin for both

Bicep does **not** require a resource group. A template's `targetScope`
(`resourceGroup` by default, or `subscription` / `managementGroup` / `tenant`)
decides where it deploys; only `resourceGroup`-scoped templates need a target
RG, while a template that *creates* resource groups must be
`targetScope = 'subscription'`. The generic build plugin (`OHK-gqvi9kv4`)
handles both with no template-specific code:

1. **Detection, not declaration.** After compiling, the engine reads the ARM
   `$schema` (`deploymentTemplate.json#` = resource group,
   `subscriptionDeploymentTemplate.json#` = subscription) via
   `detect_target_scope`. Nothing on the order form claims a scope.
2. **The `Resource Group` input is optional.** A resource-group-scoped
   template with no RG selected fails fast with a clear message (nothing is
   submitted). A subscription-scoped template ignores any RG selected and says
   so in the job output. Blueprints for subscription-scoped templates simply
   leave the field off their custom form.
3. **Addressing.** RG-scoped stacks live at
   `/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Resources/deploymentStacks/{name}`;
   subscription-scoped ones at
   `/subscriptions/{sub}/providers/Microsoft.Resources/deploymentStacks/{name}`
   and carry a `location` (see the config block). The scope is stored on the
   resource as `bicep_target_scope`; teardown, Update and Drift Check read it.
   Resources provisioned before scope support have no stored scope and are
   treated as RG-scoped.
4. **Ownership on delete.** A subscription-scoped stack owns the resource
   groups its template created, so its `actionOnUnmanage.resourceGroups` is
   `delete` (RG-scoped stacks keep `detach` — the engine never owns the
   customer's RG). Deleting the resource therefore deletes the RG. Azure will
   refuse if someone has since put unmanaged resources into that RG; the
   teardown surfaces the 409 with guidance rather than force-deleting.
5. **Management-group and tenant scopes** are recognised and rejected with a
   clear error; they remain out of scope.

Blueprints wired to the engine today:

| Blueprint | Template | Scope |
|---|---|---|
| `Bicep Deployment` (`BP-nibk4erf`) | `docs/examples/bicep/storage-account/main.bicep` in this repo | resource group |
| `Azure Resource Group - Bicep` (`BP-p7zmh96m`) | `subscription-deployments/create-rg/main.bicep` in the public [`Azure/azure-quickstart-templates`](https://github.com/Azure/azure-quickstart-templates/tree/master/subscription-deployments/create-rg) repo, ref `master` | subscription |

The public quickstart repo is anonymously readable but still fetched through
the `GitHub` ConnectionInfo (the engine always authenticates). Its `master`
ref is mutable — pin a commit SHA in the blueprint's defaults before using it
for anything beyond a demo (§7).

### Optional: offer the new resource group on the Environment

`Azure Resource Group - Bicep` has a second, optional build step
(`Add Resource Group to Environment`, `OHK-r1imfgdx`, order-form checkbox). When
ticked, the created resource group's name is added as an option of CloudBolt's
platform `resource_group_arm` parameter on the Environment the order
provisioned into (the build plugin records that environment as `bicep_env_id`),
so it appears in that environment's Resource Group dropdown for VM orders
without waiting for a resource-handler sync. Mechanically this links the
environment to a shared `CustomFieldValue`; the environments touched are
recorded on the resource (`bicep_rg_option_env_ids`).

Its teardown counterpart (`Remove Resource Group from Environments`,
`OHK-vm5p34w3`) runs when the resource is deleted and removes the option from
**every environment on the same Azure subscription (resource handler)** that
offers it, plus the recorded environments. Scope rationale: an Azure resource
group is identified by (subscription, name), so once deleted it is stale for
every region-environment of that subscription, while a same-named resource
group in another subscription lives on another handler and is untouched. Only
the environment link is removed — the `CustomFieldValue` itself is never
deleted, because it is shared across environments and may be the stored value
of that parameter on servers.

### Sparse fetch — why a 250 MB repo costs kilobytes

GitHub's tarball endpoint has no path filter, and `azure-quickstart-templates`
is well over the engine's `MAX_ARCHIVE_BYTES` guard. The engine therefore
fetches **only the template's directory** (recursively, via the GitHub
Contents API) plus any `bicepconfig.json` in its ancestor directories, and
compiles from that. The full tarball is still used, automatically, when:

- the template sits at the repository root (a sparse fetch would be the whole
  repo anyway);
- a `.bicep`/`.bicepparam` file in the folder references a parent path
  (`'../…'` in a `module`, `import`, `using` or `load*Content` reference);
- the folder exceeds the sparse caps (`MAX_SPARSE_FILES`,
  `MAX_SPARSE_FILE_BYTES`) or the Contents API cannot serve it.

So the authoring rule for large repos is simple: keep a template and
everything it references inside one directory. Registry modules (`br:`/`ts:`)
are unaffected — Bicep restores those itself at compile time.

## 5. The approval gate

- Build and day-2 Update **pause the job** after a what-if preview. Approve by
  clicking **Continue Job** on the job detail page; reject by **canceling** the
  job — what-if is read-only, so a reject submitted nothing to Azure (no
  rollback needed).
- The approval window is the **global `job_timeout` GlobalPreference** (default
  8h). Past it, the job cancels through the same reject path. Raising it
  lengthens every job's timeout, not just these.
- Resume is **cb_admin-only** (platform constraint). A cb_admin must be in the
  loop for every provision and Update.
- **Paused-job recovery:** if the jobengine restarts while a job is paused, the
  job is canceled and no cleanup code runs — but because what-if is read-only,
  nothing was submitted, so the worst case is a leaked temp checkout (swept on
  startup) and a re-runnable action. Just re-order or re-run the action.
- The what-if/plan summary is **group-visible job output**. `@secure()` values
  are redacted by the engine, but templates must not echo other sensitive values
  in outputs or what-if deltas.

## 6. Deny settings (optional, off by default)

Stack deny settings (`denyDelete` / `denyWriteAndDelete`) protect a deployment's
resources from out-of-band modification. **Off (`none`) for the POC.** Before
enabling: the engine auto-excludes its own service principal (derived at runtime
from the token) so it can still update its own stack — but confirm the SP has
not been rotated to a new object id with stale stacks still excluding the old
one, or the engine will be unable to manage those stacks without manual portal
intervention.

## 7. Production hardening (deferred follow-ups)

- **Ref-pinning is required for production.** A mutable branch ref means anyone
  who can merge to it controls the next deployment (the template runs with the
  SP's credentials). Pin a commit SHA or a signed tag.
- Least-privilege custom Azure role instead of Contributor.
- GitHub App auth instead of a PAT.
- Azure DevOps repo support, management-group-scoped templates, and GitHub
  Actions delegation are out of POC scope (subscription scope is supported —
  §4a).
