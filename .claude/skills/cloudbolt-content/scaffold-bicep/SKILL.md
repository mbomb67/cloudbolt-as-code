---
name: scaffold-bicep
description: Scaffold a CloudBolt blueprint that deploys an Azure Bicep template via the generic Bicep deployment engine. Compiles the template, walks the author through per-parameter curation (accept-and-pin vs expose-as-typed-input), and emits a blueprint plus a typed Update action wired to the shared engine plugins.
when-to-use: When the user says "scaffold a bicep blueprint", "make a catalog item for this bicep template", "wrap this .bicep in CloudBolt", or points at a Bicep template in a GitHub repo and wants an orderable, typed blueprint. Requires the Bicep deployment engine content (SHM-eybr4hgz, SHM-bbswv27r, OHK-gqvi9kv4, OHK-t2gs5caq) to already exist in the repo.
---

# scaffold-bicep

Generate an orderable CloudBolt blueprint for a specific Azure Bicep template, with typed order-form inputs curated from the template's own parameter schema. The blueprint wires to the **generic** Bicep engine plugins — this skill generates no engine logic, only the per-template blueprint, its curated inputs, and a thin typed Update adapter.

## Required reading

- [docs/agents/metadata-schemas.md](../../../docs/agents/metadata-schemas.md): §0 (universal rules), §1 (blueprints), §2 (plugins), §3 (resource_actions).
- [docs/bicep-deployment-setup.md](../../../docs/bicep-deployment-setup.md): the engine prerequisites (GitHub ConnectionInfo, Azure handler, the pinned Bicep binary config block).
- The generic engine this scaffold targets: build plugin `plugins/OHK-gqvi9kv4`, teardown `plugins/OHK-t2gs5caq`, generic Update `resource_actions/RSA-fa7r7cg7`, Drift Check `resource_actions/RSA-5jeixn92`, and the shared modules `shared_modules/SHM-eybr4hgz` (github) + `shared_modules/SHM-bbswv27r` (bicep_engine). If these are absent, stop and tell the user to build the engine first.

## When to use

- The user has a `.bicep` template in a GitHub repo and wants a catalog item where orderers pick typed values (dropdowns from `@allowed`, constrained fields, masked secure inputs) instead of pasting raw JSON.
- For a quick one-off deploy without curation, the generic `Bicep Deployment` blueprint (`BP-nibk4erf`) already works with a JSON parameters input — no scaffold needed.

## Inputs

Ask the user:

1. **GitHub repo** (`owner/repo`, required) and **template path** (e.g. `main.bicep`, required) and **ref** (branch/tag/SHA; default branch if blank). For production, recommend a pinned tag or SHA.
2. **Blueprint name** and **resource type label** (singular + plural).
3. **Parameter file** (optional): path to a `.bicepparam`/`parameters.json` in the repo whose values should be offered as the proposed defaults during curation (file value beats template default — scaffold-time only; never read at deploy).

## Procedure

### 1. Compile the template and extract the schema

The scaffold runs in the dev/agent environment (NOT on the appliance), so it bootstraps and runs Bicep locally with the **same pinned version** as the engine config block in `shared_modules/SHM-bbswv27r/SHM-bbswv27r_script.py` (`BICEP_VERSION`). Fetch the template (clone or `gh api` the archive at the ref), then:

```bash
bicep build <path>/main.bicep --stdout   # or: az bicep build --file ... --stdout
```

Parse the ARM JSON `parameters` (and `definitions` for `$ref` user-defined types) into the same normalized schema the engine's `extract_parameter_schema` produces: per parameter — `type`, `required` (no `defaultValue`), `default`, expression-default flag (`[...]`), `allowedValues`, min/max, minLength/maxLength, `@secure()` flag, description, and complex (object/array/UDT) flag.

If the template has **zero parameters**, emit an orderable blueprint with no exposed inputs and no Update action, and report that — do not error.

Also record the template's **target scope** from the compiled ARM `$schema` (`deploymentTemplate.json#` = resource group; `subscriptionDeploymentTemplate.json#` = subscription, e.g. a template that creates resource groups). The engine detects this itself at deploy time (`detect_target_scope`), but the scaffold needs it to shape the form: a **resource-group-scoped** template exposes the engine's `resource_group` input (required on the form); a **subscription-scoped** template omits it entirely (the build input is optional and ignored at that scope). Management-group/tenant scopes are unsupported — stop and tell the user. Worked subscription-scope example: `blueprints/BP-p7zmh96m` + `forms/FRM-i9zadhpc`.

### 2. Curate each parameter (the heart of this skill)

Walk the author through every parameter, one decision each:

- **No default** → always **exposed as a required input**. Do not offer "pin" (there is no value to pin).
- **Literal default** → ask **accept (pin)** or **expose**:
  - *Accept* → record `{param: <literal value>}` in the blueprint's pinned map. The orderer never sees it.
  - *Expose* → generate a typed input (see step 3), pre-filled with the default.
- **Expression default** (`[resourceGroup().location]` etc.) → offer **pin-by-omission only** (record `{param: "__bicep_omit_expression_default__"}` — the engine omits it so ARM evaluates the expression at deploy). Never present it as an editable literal or a dropdown initial value.
- **`.bicepparam`/parameters file present** → for each param the file sets, offer the file's value as the proposed default and **show its provenance** ("default from params file" vs "from template") so the author isn't surprised.
- **Complex (object/array/user-defined type)** → expose as a free-text JSON input (the engine parses it with `json.loads`); note the expected shape in the input description. If a `$ref` cannot be resolved, stop and tell the author the template's type definitions need fixing.

Record the curation result as a map of `{param: pinned-value-or-omit-sentinel}` (pinned) and a list of exposed params with their typed-input specs.

### 3. Emit the content

Generate global_ids per §0 (shell-out, grep for collisions):

```bash
python -c "import random,string,sys; a=string.ascii_lowercase+string.digits; [print(p+'-'+''.join(random.choices(a,k=8))) for p in sys.argv[1:]]" BP RSA OHK BDI BDI RT CF CF CF
```

(One `RSA` + one `OHK` for the typed Update; one `CF` per exposed param across BOTH the build inputs and the typed Update inputs; `BDI` for build + teardown items; `RT` for the resource type.)

**a. Blueprint `blueprints/BP-<id>/BP-<id>_metadata.json`** — mirror `blueprints/BP-nibk4erf` exactly (`metadata_version: "2026.1.0"`, inline `resource_type` with its `RT-` id, `resource_name_template: ""`):
- `deployment_items[]`: one item wired to the **generic build** `plugins/OHK-gqvi9kv4`, `tier_type: "plugin"`.
- `teardown_items[]`: one item wired to the **generic teardown** `plugins/OHK-t2gs5caq`, `tier_type: "teardown_plugin"`, `deploy_seq: -1`.
- Blueprint-level `parameters[]` (§1): the engine's fixed inputs pre-filled and hidden where possible — `repo`, `template_path`, `ref` set to the curated values (and `resource_group` only for resource-group-scoped templates); plus one typed parameter per **exposed** template param (dropdown `options` from `allowedValues`, `type` mapped from the Bicep type, `PWD` for `@secure()`, regex/min/max as constraints, `required` per the schema), `destination: "Resource"` so they land as resource custom fields named exactly after the template parameter (the generic build reads them by name).
- Stash the pinned map: set a blueprint parameter/default that writes `bicep_pinned_params` (JSON) onto the resource at order time, OR document that the build item's default values carry it. The generic build reads `bicep_pinned_params` and merges it.
- `management_actions[]`: wire the **generic Drift Check** `resource_actions/RSA-5jeixn92` and the **typed Update** RSA generated below. (Optionally also wire the generic JSON Update `RSA-fa7r7cg7`.)

**b. Typed Update `resource_actions/RSA-<id>/` + paired `plugins/OHK-<id>/`** — the param set is known now, so the typed inputs are static metadata (no runtime field synthesis):
- RSA: one **kebab-case** input per exposed, non-pinned param (typed, dropdowns, `PWD` for secure), each sharing a `CF-` id with the paired OHK's **snake_case** input. `enabled: true` explicit. **Do NOT set `is_synchronous`** (the approval pause needs a real job thread). No `action_inputs_sequence` on the RSA.
- OHK: a thin adapter — `run(job, resource, **kwargs)` that reads each typed input, builds the `supplied` dict, and calls the shared engine exactly like `plugins/OHK-9f45ede7` does (import from `shared_modules.bicep_engine` and `shared_modules.github`; reuse its `_current_mirrors`, concurrency fail-fast, `run_with_approval`, mirror-refresh). Pinned params are never inputs. `@secure()` inputs are `PWD` type and stored/reused via an encrypted `bicep_sec_<param>` custom field (read it when the masked input is left blank — "leave unchanged"). Declare `dependencies.sharedModules` for both modules. This adapter marshals typed inputs into the engine call; it contains no deployment logic of its own.

### 4. Handle re-runs against an evolved template

If a blueprint for this template already exists, **diff** the new schema against its curated inputs rather than silently overwriting:
- New params → prompt the author to curate them.
- Removed params → flag the now-orphaned inputs for deletion.
- Retyped/re-constrained params → re-curate.
Regenerate the blueprint AND the typed Update from the same curation result so the two never drift. Warn the author to re-confirm every choice.

### 5. Report back

List every folder created, every cross-reference wired (build/teardown/management-action hooks, shared-module deps), the pinned vs exposed parameter split, and a reminder to run `validate-metadata`. If the template uses registry modules (`br:`), remind the author the appliance needs egress to that registry at deploy time.

## Constraints

- IDs generated randomly via shell-out, never hand-typed; grep for collisions.
- Generate no engine logic — the build/teardown/run-engine live in the shared modules and generic plugins. The only generated Python is the thin typed-Update adapter, which just marshals typed inputs into the shared engine.
- Pinned parameters never appear as order-form or day-2 inputs.
- **Humanize input labels.** For every generated typed input, the `name` stays the exact Bicep parameter identifier (e.g. `storageAccountName`) but the `label` is the human-readable, title-cased form (`Storage Account Name`), matching the engine's `humanize_label` (camelCase/snake_case → words, acronyms like ID/URL/SKU uppercased). Keep raw identifiers out of labels.
- Never read a `.bicepparam` file at deploy time — it is a scaffold-time default source only.
- Use the same pinned Bicep version as the engine config block, so the schema the scaffold sees matches what the appliance will compile.
