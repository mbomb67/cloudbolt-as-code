# CloudBolt Source Control Repos — Metadata Schemas

Source-of-truth reference for every `<GLOBAL_ID>_metadata.json` shape CloudBolt's **Source Control Repos** feature (colloquially "CMP-as-Code") can emit. Every field, enum value, and cross-reference convention below is grounded in CloudBolt's source code — model classes, DRF serializers, and the export/import code paths in the `source_code_repos` Django app. Citations below name the specific source file and line in the CloudBolt CMP repo where each rule lives. When CloudBolt ships new content types or changes a schema, compare the affected serializer in your CloudBolt version against this file and update it.

> **Defaults assumed.** Top-level directory names (`plugins/`, `shared_modules/`, `resource_actions/`, etc.) below are the defaults CloudBolt ships with. They can be customized per repo via `SourceCodeRepo.path_to_*` (shell + API call), but the override is rare. Treat the defaults as the contract; only probe for customization if a default directory is missing or a cross-reference path fails to resolve.

## How skills consume this document

This document is the single source of truth for per-content-type schemas. The skills under [`.claude/skills/cloudbolt-content/`](../../.claude/skills/cloudbolt-content) reference its sections by stable anchor rather than inlining JSON templates or required-field lists. **When invoking any `scaffold-*`, `validate-metadata`, or `find-content-by-name` skill, also load this file** — the skill body intentionally omits schema facts that live here so they don't drift out of sync.

Section anchors are stable (renames break skills):

| § | Content |
|---|---|
| §0 | Universal rules — ID format, cross-reference convention, casing, export/import asymmetry, secret redaction, validation philosophy |
| §1 | `blueprints/` BP-* |
| §2 | `plugins/` OHK-* |
| §3 | `resource_actions/` RSA-* |
| §4 | `server_actions/` SVA-* |
| §5 | `orchestration_actions/` HPA-* (includes the full seeded HookPoint label list) |
| §6 | `flowcontrol_actions/` FCA-* |
| §7 | `recurring_jobs/` RJB-* |
| §8 | `cit_tests/` CIT-* |
| §9 | `webhooks/` IWH-* |
| §10 | `shared_modules/` SHM-* |
| §11 | `extensions/` XUI-* |
| §12 | `forms/` FRM-* (transitive-import only) |
| §13 | `form_functions/` FJS-* (transitive-import only) |

When CloudBolt changes a schema (new required field, new enum value, new content type), update the relevant section here. Skills inherit the change automatically — no parallel edits required.

---

## 0. Universal Rules

### ID format

- Canonical pattern: `<PREFIX>-<suffix>` where suffix is 8–12 chars from `[a-z0-9]` (base36).
- Default suffix length is **8**; configurable up to **12** via `GLOBAL_ID_SUFFIX_LENGTH`.
- Source: `common/mixins.py:376` (generation), `:443` (prefix prepend), `:523` (regex `<prefix>-[0-9a-z]{8,12}`).
- Metadata filename is always `f"{obj.global_id}_metadata.json"`.

#### How CloudBolt generates the suffix — and how to mimic it

CloudBolt's `get_global_id_chars()` (`common/mixins.py:376`) is literally:

```python
alphabet = string.ascii_lowercase + string.digits   # 36 symbols
id_chars = "".join(random.choices(alphabet, k=length))  # length defaults to 8
```

The prefix (`OHK`, `BP`, `RSA`, …) is a per-model class attribute prepended in `save()` (`:443`), idempotently. The suffix is **pure entropy** — `k` independent random draws. It carries **no meaning**: it is not derived from the content's name, type, or creation order.

**When scaffolding, generate suffixes the same way — by running CloudBolt's actual algorithm, never by typing them yourself.** An agent hand-authoring a "random" string inevitably encodes semantics (`OHK-s3bld001`, `BDI-teardown1`, sequential counters), which is immediately identifiable as not-CloudBolt-generated and defeats the point of an opaque portable ID. Shell out instead — one ID per prefix you need:

```bash
python -c "import random,string,sys; a=string.ascii_lowercase+string.digits; [print(p+'-'+''.join(random.choices(a,k=8))) for p in sys.argv[1:]]" BP OHK OHK OHK RSA OHK BDI BDI
```

This applies to **every** generated ID, including nested ones that are not folder names — `BDI-` (deployment/teardown items) and `CF-` (custom fields / action inputs).

**Uniqueness is the generator's job, not CloudBolt's.** CloudBolt does not enforce uniqueness at generation time and `global_id` is not `unique=True` at the mixin level (the lookup helper at `common/mixins.py:472` even handles `MultipleObjectsReturned`). Default entropy is only 36⁸ ≈ 2.8 × 10¹², so after generating, grep the repo and regenerate on any collision. For very large repos, raise the draw to `k=12` (36¹² ≈ 4.7 × 10¹⁸).

These IDs use the non-cryptographic `random` PRNG — they are identifiers, **not** security tokens. Never treat a `global_id` as unguessable (contrast webhook tokens, which use `secrets.token_urlsafe`).

### Cross-reference convention

Every cross-reference between content units is a **repo-relative path string** of the form `"<top_level_dir>/<GLOBAL_ID>"`. Never a bare ID, numeric ID, or embedded object.

| Source content type | Dependency key path | Target | Casing |
|---|---|---|---|
| Blueprint | `deployment_items[].dependencies.hook` | `plugins/OHK-*` | snake_case |
| Blueprint | `deployment_items[].dependencies.blueprint` | `blueprints/BP-*` (sub-blueprint) | snake_case |
| Blueprint | `deployment_items[].dependencies.rate_hook` | `plugins/OHK-*` | snake_case |
| Blueprint | `deployment_items[].dependencies.environment_selection_hook` | `plugins/OHK-*` | snake_case |
| Blueprint | `teardown_items[].dependencies.hook` | `plugins/OHK-*` | snake_case |
| Blueprint | `discovery_plugin.dependencies.hook` | `plugins/OHK-*` | snake_case |
| Blueprint | `management_actions[].dependencies.resource_action` | `resource_actions/RSA-*` | snake_case |
| Blueprint | `parameters[].gen_options_hooks[].dependencies.*` | `plugins/OHK-*` | snake_case |
| Blueprint | `dependencies.custom_form` | `forms/FRM-*` | snake_case |
| Higher-level action (RSA/SVA/HPA/FCA/RJB/IWH/CIT) | `dependencies.hook` | `plugins/OHK-*` | snake_case key |
| Higher-level action | `dependencies.displayCondition` | `plugins/OHK-*` | **camelCase** |
| Higher-level action | `dependencies.custom_form` | `forms/FRM-*` | snake_case |
| Higher-level action | `dependencies.sharedModules[]` | `shared_modules/SHM-*` | **camelCase** |
| Plugin (CloudBoltHook only) | `dependencies.sharedModules[]` | `shared_modules/SHM-*` | **camelCase** |
| Shared module | `dependencies.sharedModules[]` | `shared_modules/SHM-*` | **camelCase** |
| Form | `dependencies.form_functions[]` | `form_functions/FJS-*` | snake_case |
| Orchestration action (HPA) | `hook_point` | `cbhooks.HookPoint.label` (string, not a path) | n/a |

### `action_inputs[]` key-casing rule

`action_inputs[]` items appear in many content types, with **inconsistent key casing across serializer lineages**:

| Lineage | Used by | Key casing |
|---|---|---|
| `BaseActionSerializer` (`orchestration_hook.py:1147`) | `plugins/OHK-*`, `shared_modules/SHM-*` | **snake_case** (`allow_multiple`, `hide_if_default_value`, …) |
| `HasBaseActionSerializer` via `_generate_action_input_dict` (`serializers.py:754`) | `resource_actions/`, `server_actions/`, `orchestration_actions/`, `flowcontrol_actions/`, `recurring_jobs/`, `webhooks/`, `cit_tests/` | **kebab-case** (`allow-multiple`, `hide-if-default-value`, …) |

When editing an existing file, match the casing already present.

#### Shared `action_inputs[]` item fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | `global_id` of the HookInput/CustomField (e.g. `CF-...`). |
| `name` | string | Auto-suffix `_a<hookid>` where `<hookid>` is the action's Django pk (**always digits**). The input's own `name` is stripped to bare on export; dependency-entry refs keep the suffix and on import are rewritten `_a\d+ → _a{action.id}` then matched exactly. A non-digit suffix (e.g. `_a8r1`) fails the `_a\d+$` rewrite, matches no field, and the dependency is **silently dropped on import** — see [Parameter dependencies](#parameter-dependencies-field_dependency__set). |
| `label`, `description`, `placeholder` | string / null | |
| `type` | string (enum) | CustomField ATTR_TYPES (see below). |
| `allow_multiple`, `required`, `show_on_servers`, `available_all_servers`, `show_as_attribute` | boolean | Last three stripped on import. |
| `hide_if_default_value` | boolean | Default `true`. |
| `value_pattern_string` / `formatter_pattern` | string / null | |
| `relevant_osfamilies` | array[object] | `[{"name": ...}]`. |
| `global_options` | array | |
| `field_dependency_controlling_set` / `field_dependency_dependent_set` | array[object] | Parameter dependencies (show/hide and dynamic option regeneration). Recreated on import. **Required** whenever a `generate_options_for_*` method takes `control_value`/`control_value_dict` — see [Parameter dependencies](#parameter-dependencies-field_dependency__set). |
| `minimum`, `maximum`, `regex_constraint` | string | Only when a constraint exists. |
| `gen_options_hooks` | array[object] | `[{"name","enabled"}]`; referenced actions exported as sibling files. |

#### `action_inputs[].type` (ATTR_TYPES)

Source: `infrastructure/models.py:489-510`. Allowed values:

`STR`, `INT`, `IP`, `DT`, `DTM`, `TXT`, `ETXT`, `CODE`, `BOOL`, `DEC`, `NET`, `PWD`, `TUP`, `LDAP`, `URL`, `NSXS`, `NSXE`, `STOR`, `FILE`, `AAP`.

### Parameter dependencies (`field_dependency_*_set`)

Order-form fields can drive one another: selecting a tenant repopulates the billing-account dropdown; ticking "Add extra disks" reveals disk-size fields. CloudBolt models every such relationship as a **field dependency** recorded in the `action_inputs[]` metadata. This applies to any content type that has `action_inputs[]` / `parameters[]` (plugins `OHK-*`, the action types, and blueprint parameters).

**The code is not enough.** Writing a `generate_options_for_<param>(field, control_value=..., control_value_dict=...)` method only defines *how* to compute options once the controller values arrive. It does **not** tell CloudBolt that the field has controllers. Without the matching metadata dependency, the order form never feeds the other fields' values into `control_value`/`control_value_dict` and never re-fires the generator when they change — so `control_value_dict` arrives empty and the dropdown is permanently stuck on its "select X first" placeholder. **Every dynamic-options method that consumes a controller value MUST have a corresponding `REGENOPTIONS` dependency declared in metadata.**

#### The three dependency types

`DEPENDENCY_TYPE_CHOICES` (`infrastructure/models.py`):

| `dependency-type` | Effect on the **dependent** field | Controlling field is typically | Needs a `generate_options_*` method? |
|---|---|---|---|
| `REGENOPTIONS` | Re-runs the dependent field's option generator, feeding it the controller's current value. | Any field whose value parameterizes the lookup (env, tenant, billing account…). | **Yes** — this is the whole point. |
| `SHOWHIDE` | Shows the dependent field **only when** the controller matches the trigger value. | A `BOOL` toggle (e.g. `extra_disks` controls whether `disk_1_size`, `disk_2_size` appear). | No. |
| `HIDE` | Inverse of `SHOWHIDE` — hides the dependent field when the controller matches. | A `BOOL` toggle. | No. |

#### How a dependency is recorded — the dual-entry rule

A single relationship "**field C controls field D**" is one dependency object, but it is stored **twice** — once on each field — with identical content:

- On the **controlling** field C, the object goes in `C.field_dependency_dependent_set` (read: "fields *dependent on* me").
- On the **dependent** field D, the same object goes in `D.field_dependency_controlling_set` (read: "fields *controlling* me").

Both copies must be present and must agree. The array name encodes the field's *role* in that relationship, not the contents of the object (the object always names both ends). Mnemonic:

```
field_dependency_dependent_set   → "things that DEPEND ON this field" (this field is the controller)
field_dependency_controlling_set → "things that CONTROL this field"   (this field is the dependent)
```

#### The dependency object shape

```json
{
    "controlling-field": { "name": "source_tenant_a299" },
    "dependent-field":   { "name": "billing_account_a299" },
    "dependency-type":   "REGENOPTIONS",
    "custom-field-options": [],
    "minimum": null,
    "maximum": null,
    "regex": ""
}
```

- **`controlling-field.name` / `dependent-field.name`** — the field `name`s, but carrying the `_a<hookid>` suffix that the bare top-level `"name"` has stripped on export (see [Shared `action_inputs[]` item fields](#shared-action_inputs-item-fields), `name` row). Within one action all dependency refs share the same suffix. The exact suffix value is instance-specific and CloudBolt re-derives it on import (the dependency objects are "recreated on import"); what must be correct is that each name matches an `action_inputs[].name` (modulo suffix) and that controller↔dependent point the right way.
- **`custom-field-options`** — empty for `REGENOPTIONS`. For `SHOWHIDE`/`HIDE` it carries the controller value(s) that trigger the show/hide.
- **`minimum` / `maximum` / `regex`** — `null`/`""` unless the dependency also imposes a constraint.

#### Code ↔ metadata correspondence

The generator signature must match the number of declared controllers:

| Controllers declared in metadata | Generator signature | How values arrive |
|---|---|---|
| none | `generate_options_for_x(**kwargs)` | — |
| exactly one | `generate_options_for_x(field, control_value=None, **kwargs)` | `control_value` = the single controller's value |
| one or more | `generate_options_for_x(field, control_value=None, control_value_dict=None, **kwargs)` | `control_value_dict[<bare controller name>]` per controller |

`control_value_dict` is keyed by the **bare** field name (`control_value_dict.get("source_tenant")`), even though the metadata refs are suffixed. Prefer `control_value_dict` whenever there are ≥2 controllers (it is the only way to read more than one); it also works for a single controller. Always guard with `if not control_value_dict: return [("", "------ select … first ------")]` so the field degrades gracefully before its controllers are set.

#### Worked example — `plugins/OHK-96zebx6i` (Build Azure Subscription)

A four-link `REGENOPTIONS` chain: `source_tenant → billing_account → invoice_section`, plus `source_tenant → invoice_section` (invoice section needs both), and a separate `destination_tenant → management_group`.

`source_tenant` is a pure controller (no controllers itself), so its generator takes no control args and it carries only a `dependent_set`:

```json
// action_inputs[] entry for source_tenant
"field_dependency_controlling_set": [],
"field_dependency_dependent_set": [
    { "controlling-field": {"name": "source_tenant_a299"}, "dependent-field": {"name": "billing_account_a299"}, "dependency-type": "REGENOPTIONS", "custom-field-options": [], "minimum": null, "maximum": null, "regex": "" },
    { "controlling-field": {"name": "source_tenant_a299"}, "dependent-field": {"name": "invoice_section_a299"}, "dependency-type": "REGENOPTIONS", "custom-field-options": [], "minimum": null, "maximum": null, "regex": "" }
]
```

`invoice_section` is a pure dependent with **two** controllers, so it carries only a `controlling_set` with two entries, and its generator reads both via `control_value_dict`:

```json
// action_inputs[] entry for invoice_section
"field_dependency_controlling_set": [
    { "controlling-field": {"name": "source_tenant_a299"},   "dependent-field": {"name": "invoice_section_a299"}, "dependency-type": "REGENOPTIONS", "custom-field-options": [], "minimum": null, "maximum": null, "regex": "" },
    { "controlling-field": {"name": "billing_account_a299"}, "dependent-field": {"name": "invoice_section_a299"}, "dependency-type": "REGENOPTIONS", "custom-field-options": [], "minimum": null, "maximum": null, "regex": "" }
],
"field_dependency_dependent_set": []
```

```python
def generate_options_for_invoice_section(field, control_value=None, control_value_dict=None, **kwargs):
    if not control_value_dict:
        return [("", "------ Select billing account first ------")]
    source_rh_id    = control_value_dict.get("source_tenant")     # bare names as keys
    billing_account = control_value_dict.get("billing_account")
    ...
```

`billing_account` sits in the middle of the chain, so it appears in **both** sets: its `controlling_set` records that `source_tenant` controls it, and its `dependent_set` records that it controls `invoice_section`.

#### Authoring checklist

1. Decide direction for each pair: which field's value parameterizes the other's options (`REGENOPTIONS`) or visibility (`SHOWHIDE`/`HIDE`).
2. For each relationship, add the **same** object to the controller's `field_dependency_dependent_set` **and** the dependent's `field_dependency_controlling_set`.
3. Reference fields by their suffixed `name` (`<name>_a<hookid>`), where `<hookid>` is **digits**. On import CloudBolt rewrites the trailing `_a\d+` to the importing action's own pk and then matches the full name exactly (`FieldDependencySerializer.get_or_create_from_dict`, `infrastructure/serializers.py`). The digits you author are therefore **discarded and replaced** — mirror an exemplar (`_a357`) or use any integer like `_a1`; the value need not match anything and the two mirrored copies need not share the same integer. But the suffix on each ref **must match `_a\d+$`**: a non-digit token like `_a8r1` is left unrewritten, the `CustomField` lookup misses, and that one dependency is dropped with a silent `return None` (the dependent generator then never receives `control_value`/`control_value_dict`, so its options never refresh).
4. Give every `REGENOPTIONS` dependent a `generate_options_for_<name>` method whose signature matches its controller count; read controllers from `control_value_dict` by **bare** name.
5. Order `action_inputs_sequence` so controllers precede their dependents.
6. Run the `validate-metadata` skill to catch dangling field refs, one-sided (un-mirrored) entries, and non-integer suffixes.

#### Common failure: the dependency silently disappears on import

The JSON looks correct, but in the running UI the dependent field's options never refresh — its generator always returns the "select … first" fallback, and `control_value` / `control_value_dict` arrive empty. CloudBolt **dropped the dependency on import**. Diagnostic signature: re-export (round-trip) and the `field_dependency_*_set` arrays come back empty `[]`.

Mechanism (`FieldDependencySerializer.get_or_create_from_dict`, `infrastructure/serializers.py`): on import each `controlling-field.name` / `dependent-field.name` is rewritten with `re.sub(r"_a\d+$", "_a{action.id}", name)`, then looked up by exact `CustomField.name`; a miss returns `None` and silently skips that one entry (no error, no log). The action and its inputs still import fine — you just lose the dependency. Causes:

- **Non-digit suffix** (e.g. `_a8r1`) — fails the `_a\d+$` regex, so it is *not* rewritten, the literal `env_id_a8r1` matches no field, and the entry is dropped. The most common hand-authoring mistake: the engine code can correctly declare `generate_options_for_x(field, control_value=None, ...)`, but a malformed suffix means the UI never passes `control_value`, so that code path is dead. The digits' value is irrelevant (it's overwritten on import) — but it must be digits.
- **Bare stem with no matching input** — after the rewrite, the stem (e.g. `env_id`) must equal an `action_inputs[].name` in the same action, or the lookup misses.
- **One-sided entry** — present on only one of the two mirrored fields (see checklist step 2). (The two copies are rewritten independently and need *not* share the same integer — each just has to be digit-suffixed and resolve.)

When unsure, wire the dependency in the CloudBolt UI and export — the UI always emits a valid suffix. Authoring the field dependency *and* its generator method is not enough; the suffix must match `_a\d+$` or the two never connect.

### Export/import asymmetry — two registries

CloudBolt has **two distinct registries**, and the new instructions must teach this:

- **Export/push registry** — `model_repo_mapping` at `source_code_repos/models.py:1093`. Covers all 13 types. This is "what can be pushed to a repo."
- **Sync/import registry** — `OBJECT_TYPE_MAPPINGS` at `source_code_repos/services.py:44`. Covers 11 top-level sync targets (10 action/extension types + blueprints handled separately). Does **NOT** list `forms/` or `form_functions/`.

**Implication.** When you create a new `forms/FRM-*` or `form_functions/FJS-*` folder, it must be referenced from at least one parent (a blueprint/action `dependencies.custom_form` for a form, or a form's `dependencies.form_functions[]` for a function). Otherwise CloudBolt will never import it — it sits on disk untouched.

### Secret redaction — round-trip is NOT lossless

Secrets are replaced with literal placeholder strings on export. The importer skips re-applying placeholder values, so **secrets must be re-entered after each sync**:

| Placeholder | Used by |
|---|---|
| `"YOUR_CREDENTIALS"` | `RemoteScriptHook.credentials`, `CopyFileAction.credentials` |
| `"YOUR_AUTH_INFO"` | `WebHook.auth_header_value` |
| `"YOUR_EMAIL_INFO"` | `EmailHook.from_address`, `EmailHook.send_to_address` |
| `"SOURCE_CODE_URL Redacted"` | Any URL-sourced script's `source_code_url` |

Other round-trip caveats apply across all types — see the per-type "Round-trip caveats" subsections.

### Validation

CloudBolt ships **no JSON Schema and no Pydantic models** for these files. Validation is "whatever the DRF serializer accepts." The strictest contract is the union of (a) each serializer's required-field validators and (b) what its `*_from_local_path` / `create_resource_from_metadata` actually reads. The `validate-metadata` skill in `.claude/skills/cloudbolt-content/` performs structural validation (required fields, cross-reference integrity, enum values, casing) — not full semantic validation.

---

## 1. `blueprints/` — BP-*

- **Django model:** `servicecatalog.ServiceBlueprint` at `servicecatalog/models.py:173`.
- **Serializer:** `ServiceBlueprintSerializer` at `servicecatalog/api/v3/serializers/service_blueprint.py:29`; export/import mixin at `service_blueprint_import_export_mixin.py:557`.
- **Top-level sync target?** Yes.
- **Colocated files:** Only an optional icon image (named by metadata `icon` key). All sub-content (plugins, RSAs, forms, etc.) is exported to its own top-level folder and referenced by path string.

### Required fields

| Field | JSON type | Notes |
|---|---|---|
| `id` | string | `BP-<id>`. |
| `metadata_version` | string | **Must be `"2026.1.0"`** (or current schema version). Without this field a blueprint is NOT recognized on import — CloudBolt's sync skips it silently. Mirror whatever value other blueprints in this repo carry. |
| `name` | string | Required; Django dedup suffixes stripped on export. |
| `description` | string | Import default `""`. |
| `any_group_can_deploy` | boolean | Default `false`. |
| `sequence` | integer | Default `0`. |
| `favorited` | boolean | Default `false`. |
| `resource_name_template` | string | Import default `""`. |
| `is_orderable` | boolean | Default `true`. |
| `auto_historical_resources` | boolean | Default `false`. |
| `show_recipient_field_on_order_form` | boolean | Display metadata only (not re-applied on import). |
| `labels` | array[object] | Blueprint categories/tags. |
| `deployment_permissions` | array[string] | `CBPermission` names. |
| `deployment_items` | array[object] | Build-tab service items — see nested schema. |
| `teardown_items` | array[object] | Teardown-tab items (same schema). |
| `management_actions` | array[object] | Resource actions on the Management tab — see nested schema. |
| `last_updated` | string `YYYY-MM-DD` | Export stamp; not read on import. |
| `minimum_version_required` | string | Default `"8.6"`. |
| `maximum_version_required` | string | Default `""`. |

### Optional fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `icon` | string | absent | Basename of the colocated icon image. |
| `resource_type` | object | absent | Full `ResourceType` dict (from `ResourceTypeSerializer`). |
| `parameters` | array[object] | absent | Blueprint-level parameters — see nested schema. |
| `discovery_plugin` | object | absent | `{href, title, dependencies:{hook}}`. |
| `has_custom_form` | boolean | absent | Set when a custom form is attached. |
| `custom_form` | string | absent | Custom-form `global_id` (not the path). |
| `dependencies` | object | absent | Top-level `{custom_form: "forms/FRM-..."}`. |

`groups_that_can_deploy` / `groups_that_can_manage` are instance-specific and dropped from repo exports (`instance_specific_info=False`). `last_cached` and `remote_source_url` are explicitly deleted from export.

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| `deployment_items[].dependencies.hook` | `plugins/OHK-*` | The build plugin for the item. |
| `deployment_items[].dependencies.blueprint` | `blueprints/BP-*` | Sub-blueprint; exported recursively. |
| `deployment_items[].dependencies.rate_hook` | `plugins/OHK-*` | |
| `deployment_items[].dependencies.environment_selection_hook` | `plugins/OHK-*` | Server items. |
| `teardown_items[].dependencies.hook` | `plugins/OHK-*` | |
| `discovery_plugin.dependencies.hook` | `plugins/OHK-*` | |
| `management_actions[].dependencies.resource_action` | `resource_actions/RSA-*` | |
| `parameters[].gen_options_hooks[].dependencies.*` | `plugins/OHK-*` | |
| `dependencies.custom_form` | `forms/FRM-*` | Top-level blueprint custom form. |

### Nested schemas

**`deployment_items[]` / `teardown_items[]`** — shared schema (`service_item.py:156` + `common_base.py:17`):

| Key | Type | Notes |
|---|---|---|
| `id` | string | SI `global_id`; build items prefix `BDI-`. |
| `name`, `description` | string | |
| `deploy_seq` | integer | Ordering. |
| `tier_type` | string | Polymorphic discriminator — see enums. |
| `execute_in_parallel`, `show_on_order_form` | boolean | Import defaults `false`. |
| `rate` | string / null | |
| `action_name`, `rate_action_name` | string | For action SIs. |
| `dependencies` | object | See cross-references. |
| `parameter_defaults` | array `{name,label,value}` | For SIs with input mappings. |
| `continue_on_failure` | boolean | Action items; default `false`. |
| `run_on_scale_up` | boolean | Action items; default `true`. |
| `enabled` | boolean | Action items. |
| `server_tiers` | array[string] | |

Polymorphic extras by `tier_type`:
- `provserver`: `restrict_applications`, `hostname_template`, `all_environments_enabled`, `os_build`, `allowed_os_families`, `applications`, `environment_selection_orchestration`.
- `tfconfig`: `local_path`, `module_file`, `git_source_code_url`, `source_code_url`, `branch`, `plan_directory`, `preserve_config_dir`, `refresh_on_order`.
- `tfoperation`: `resource_type`, `working_directory` (`source_type` ∈ {`REPO`, `ZIP`, `LOCAL`}), `variable_maps`, `plan_directory`, `refresh_on_order`, `default_tfvars`, `terraform_version`, `confirm_terraform_plan`.

**`management_actions[]`** — stripped export form:

| Key | Type |
|---|---|
| `enabled` | string `"True"` / `"False"` (yes, stringified) |
| `label` | string |
| `dependencies` | `{resource_action: "resource_actions/RSA-*"}` |

**`parameters[]`** (`service_blueprint.py:197`) — `name`, `label`, `description`, `type` (CF datatype), `required`, `show_as_attribute`, `show_on_servers`, `available_all_servers`, `global_options`, `relevant_osfamilies`, `field_dependency_*_set`, plus: `destination` (`"Resource"` | `"Build Items"` | `"Both"`), `options` (array), `constraints` (object or literal `"Unconstrained"`); preconfigs add `constrained_options` (array or literal `"No options"`).

### Enums

- `deployment_items[].tier_type` (`service_item.py:136-151`): `blueprint`, `copy`, `pod`, `loadbalancer`, `network`, `server`/`provserver`, `plugin`, `email`, `workflow`/`flow`, `script`, `terraform`, `webhook`, `tfconfig`, `tfoperation`.
- `parameters[].destination`: `"Resource"` | `"Build Items"` | `"Both"`.
- `parameters[].constraints`: object or literal `"Unconstrained"`.
- `parameters[].constrained_options`: array or literal `"No options"`.
- `tfoperation` `working_directory.source_type`: `REPO` | `ZIP` | `LOCAL`.

### Round-trip caveats

- `groups_that_can_deploy` / `groups_that_can_manage` are dropped on export.
- `last_cached`, `remote_source_url`, `blueprint_image` URL are dropped (the image is referenced by `icon` filename instead).
- `last_updated` is export-only and not consumed on import.

### Worked example

```json
{
    "any_group_can_deploy": false,
    "auto_historical_resources": false,
    "deployment_items": [
        {"id": "BDI-a1b2c3d4", "name": "Provision Server", "description": "", "deploy_seq": 1, "tier_type": "provserver", "execute_in_parallel": false, "show_on_order_form": true, "rate": null, "restrict_applications": false, "hostname_template": "", "all_environments_enabled": false, "os_build": null, "allowed_os_families": null, "applications": null, "environment_selection_orchestration": null},
        {"id": "BDI-e5f6g7h8", "name": "Run My Plugin", "description": "", "deploy_seq": 2, "tier_type": "plugin", "execute_in_parallel": false, "show_on_order_form": false, "continue_on_failure": false, "run_on_scale_up": true, "enabled": true, "rate": null, "action_name": "My Plugin", "dependencies": {"hook": "plugins/OHK-xxxxxxxx"}}
    ],
    "teardown_items": [],
    "description": "An example blueprint.",
    "discovery_plugin": {"href": "/api/v3/cmp/actions/OHK-discvxxx/", "title": "Discover Resources", "dependencies": {"hook": "plugins/OHK-discvxxx"}},
    "favorited": false,
    "icon": "my-blueprint-icon.png",
    "id": "BP-ap5jo4mo",
    "is_orderable": true,
    "labels": [],
    "last_updated": "2026-05-29",
    "management_actions": [{"label": "Restart Service", "enabled": "True", "dependencies": {"resource_action": "resource_actions/RSA-xxxxxxxx"}}],
    "maximum_version_required": "",
    "minimum_version_required": "8.6",
    "name": "My Blueprint",
    "deployment_permissions": ["order.submit"],
    "resource_type": {"name": "service", "label": "Service", "plural_label": null, "icon": "", "lifecycle": "ACTIVE", "list_view_columns": []},
    "sequence": 0,
    "show_recipient_field_on_order_form": false
}
```

---

## 2. `plugins/` — OHK-*

All eight plugin subtypes share the **`OHK-` prefix**. The subtype is carried in the metadata `type` field, not the ID:

| `type` string | Django model | `type_slug` |
|---|---|---|
| `"CloudBolt Plug-in"` | `CloudBoltHook` | `plugin` |
| `"Email Hook"` | `EmailHook` | `emailhook` |
| `"External Flow"` | `FlowHook` | `flow` |
| `"Remote Script"` | `RemoteScriptHook` | `script` |
| `"Copy File"` | `CopyFileAction` | `copy` |
| `"Webhook"` | `WebHook` | `webhook` |
| `"Terraform Plan"` | `TerraformPlanHook` | `terraform_plan` |
| `"Shared Module"` | `SharedModule` | `shared_module` (but lives under `shared_modules/`, not `plugins/`) |

The base `OrchestrationHook` has no import mapping — a file with `type: "Orchestration Hook"` cannot be re-imported.

- **Serializer:** `BaseActionSerializer` at `cbhooks/api/v3/serializers/orchestration_hook.py:49`.
- **Top-level sync target?** Yes.
- **Colocated files:** Exactly one — `<OHK-id>_script.py` (named by metadata `script_filename`). Written in text mode except `TerraformPlanHook` (binary). On import, `script_filename` is required for `type_slug ∈ {plugin, script, copy, shared_module}` unless `source_code_url` is present.

### Required fields

| Field | JSON type | Notes |
|---|---|---|
| `name` | string | Unique on `OrchestrationHook.name`. |
| `id` | string | `OHK-<id>` (overrides numeric id). |
| `type` | string (enum) | Subtype discriminator (see table above). |
| `max_retries` | integer | Default `0`. |
| `description` | string | May be `""`. |
| `shared` | boolean | Model default `false`; import default `true`. |
| `script_filename` | string | `<id>_script.py`. |

### Optional fields

| Field | Type | Default (import) | Notes |
|---|---|---|---|
| `minimum_version_required` | string | `"8.6"` | |
| `maximum_version_required` | string | `""` | |
| `target_os_families` | array[string] | `[]` | CloudBoltHook/CopyFile/RemoteScript only. |
| `resource_technologies` | array[string] | `[]` | |
| `action_inputs` | array[object] | — | Snake_case keys (see Universal Rules §0). |
| `action_inputs_sequence` | array[string] | `[]` | |
| `source_code_url` | string | — | Alternative to colocated script. |
| `destination_path`, `size` | string / int | — | CopyFileAction. |
| `execution_timeout`, `commandline_args`, `remove_after_run`, `run_with_sudo` | mixed | model defaults | RemoteScriptHook. |
| `credentials` | string / object | `"YOUR_CREDENTIALS"` | RemoteScript/CopyFile (redacted). |
| `payload`, `url`, `custom_headers`, `http_method`, `content_type`, `authentication_method`, `auth_header_name`, `http_username`, `auth_header_value` | mixed | model defaults | WebHook; `auth_header_value` redacted to `"YOUR_AUTH_INFO"`. |
| `from_address`, `send_to_address` | string | `"YOUR_EMAIL_INFO"` | EmailHook (redacted). |
| `subject`, `body`, `send_to_job_owner` | mixed | — | EmailHook. |
| `local_path`, `plan_directory` | string / null | — | TerraformPlanHook. |
| `dependencies` | object | — | `{"sharedModules": [...]}` (CloudBoltHook only). |

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| `dependencies.sharedModules[]` | `shared_modules/SHM-*` | **camelCase key.** CloudBoltHook only. |
| `script_filename` | sibling file | `<OHK-id>_script.py`. |
| `source_code_url` | remote URL | string, not a content-unit path. |

### Subtype enums (WebHook)

- `authentication_method` (`cbhooks/models.py:3561`): `none`, `basic`, `token`.
- `http_method` (`:3566`): `delete`, `get`, `head`, `patch`, `post`, `put`.
- `content_type` (`:3575`): `application/x-www-form-urlencoded`, `application/json`, `application/xml`.

### Round-trip caveats

- `credentials` (RemoteScript/CopyFile), `auth_header_value` (WebHook), `from_address`/`send_to_address` (EmailHook), and `source_code_url` are redacted on export. Importer skips placeholders. Re-enter after each sync.

### Worked example

```json
{
    "action_inputs": [
        {"allow_multiple": false, "available_all_servers": false, "description": "", "field_dependency_controlling_set": [], "field_dependency_dependent_set": [], "global_options": [], "hide_if_default_value": true, "id": "CF-a1b2c3d4", "label": "Region", "name": "region", "placeholder": null, "relevant_osfamilies": [], "required": true, "show_as_attribute": false, "show_on_servers": false, "type": "STR", "value_pattern_string": null}
    ],
    "description": "Provisions a thing.",
    "id": "OHK-abcd1234",
    "max_retries": 0,
    "maximum_version_required": "",
    "minimum_version_required": "8.6",
    "name": "My Plugin Action",
    "resource_technologies": [],
    "script_filename": "OHK-abcd1234_script.py",
    "shared": false,
    "target_os_families": [],
    "type": "CloudBolt Plug-in"
}
```

A `CloudBoltHook` with shared-module deps also carries `"dependencies": {"sharedModules": ["shared_modules/SHM-11112222"]}`.

---

## 3. `resource_actions/` — RSA-*

- **Django model:** `cbhooks.ResourceAction` at `cbhooks/models.py:2409`.
- **Serializer:** `ResourceActionSerializer` at `cbhooks/api/v3/serializers/resource_action.py:22`.
- **Top-level sync target?** Yes.
- **Standalone or linked?** Either. Linked to a blueprint via that blueprint's `management_actions[].dependencies.resource_action`; standalone simply has no parent reference.
- **Colocated files:** `<RSA-id>_metadata.json` + `<RSA-id>_script.py` (script_filename). Optional shared-module deps; optional custom form.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `label` | string (≤255) | Required, non-empty; import match key. |
| `enabled` | boolean | Default `false`; may be overridden by auto-enable. |
| `requires_approval` | boolean | Default `false`. |
| `minimum_version_required` | string | Import default `"8.6"`. |

### Optional fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | string | — | `RSA-<id>`. |
| `last_updated` | date | export time | |
| `extra_classes` | string | `"fas fa-bolt"` | |
| `dialog_message` | string | `""` | |
| `submit_button_label` | string | `""` | |
| `dangerous` | boolean | `false` | |
| `is_synchronous` | boolean | `false` | |
| `auto_submit_order` | boolean | `false` | |
| `allow_scheduling` | boolean | `false` | |
| `sequence` | integer / null | `null` | Display order. |
| `new_tab_url` | string / null | `null` | |
| `list_view_visible` | boolean | `true` | "Allow Bulk Operation". |
| `dialog_title_template` | string | `""` | |
| `always_allow_to_run` | boolean | `false` | |
| `description`, `maximum_version_required`, `base_action_name`, `action_inputs` (kebab-case), `action_inputs_sequence`, `action_input_default_values`, `has_custom_form`, `icon`, `dependencies` | — | — | Inherited shared fields. |

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| `dependencies.hook` | `plugins/OHK-*` | Always present. |
| `dependencies.custom_form` | `forms/FRM-*` | If attached. |
| `dependencies.sharedModules[]` | `shared_modules/SHM-*` | camelCase key. |

RSA's display-condition FK is NOT exported (unlike `ServerAction`).

### Worked example

```json
{
    "id": "RSA-a1b2c3d4",
    "type": "CloudBolt Plug-in",
    "base_action_name": "Resize Volume",
    "label": "Resize Volume",
    "enabled": true,
    "requires_approval": false,
    "dangerous": false,
    "is_synchronous": false,
    "auto_submit_order": false,
    "allow_scheduling": false,
    "list_view_visible": true,
    "always_allow_to_run": false,
    "extra_classes": "fas fa-bolt",
    "dialog_message": "",
    "submit_button_label": "",
    "dialog_title_template": "",
    "description": "Resize an attached volume",
    "minimum_version_required": "8.6",
    "maximum_version_required": "",
    "script_filename": "RSA-a1b2c3d4_script.py",
    "last_updated": "2026-05-29",
    "action_inputs": [],
    "action_input_default_values": [],
    "dependencies": {"hook": "plugins/OHK-xxxxxxxx"}
}
```

---

## 4. `server_actions/` — SVA-*

- **Django model:** `cbhooks.ServerAction` at `cbhooks/models.py:2704`.
- **Serializer:** `ServerActionSerializer` at `cbhooks/api/v3/serializers/server_action.py:25`.
- **Top-level sync target?** Yes.
- **Standalone or linked?** Either. SVAs are typically standalone (not on a blueprint's management_actions).
- **Colocated files:** `<SVA-id>_metadata.json` + `<SVA-id>_script.py`. Two SVA-specific extras: (1) optional display-condition plugin sub-export (prefix `condition_`); (2) optional custom form.

### Required fields

Identical to RSA: `label` (string, required), `enabled` (boolean), `requires_approval` (boolean), `minimum_version_required` (string).

### Optional fields

Same ButtonActionMixin set as RSA — `id`, `last_updated`, `extra_classes`, `dialog_message`, `submit_button_label`, `dangerous`, `is_synchronous`, `auto_submit_order`, `allow_scheduling`, `sequence`, `new_tab_url`, `list_view_visible`, `dialog_title_template` (default `"Run {{action.label}} on {{server.hostname}}"`), `always_allow_to_run`, plus inherited shared fields.

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| `dependencies.hook` | `plugins/OHK-*` | Always. |
| `dependencies.displayCondition` | `plugins/OHK-*` | **camelCase.** Optional; bundled as `condition_`-prefixed sub-export. |
| `dependencies.custom_form` | `forms/FRM-*` | If attached. |
| `dependencies.sharedModules[]` | `shared_modules/SHM-*` | camelCase. |

### Worked example

```json
{
    "id": "SVA-9f8e7d6c",
    "type": "CloudBolt Plug-in",
    "base_action_name": "Run Patch Scan",
    "label": "Run Patch Scan",
    "enabled": true,
    "requires_approval": false,
    "allow_scheduling": true,
    "list_view_visible": true,
    "sequence": 10,
    "new_tab_url": null,
    "dialog_title_template": "Run {{action.label}} on {{server.hostname}}",
    "minimum_version_required": "8.6",
    "maximum_version_required": "",
    "script_filename": "SVA-9f8e7d6c_script.py",
    "last_updated": "2026-05-29",
    "action_inputs": [],
    "action_input_default_values": [],
    "dependencies": {"hook": "plugins/OHK-xxxxxxxx"}
}
```

---

## 5. `orchestration_actions/` — HPA-*

Note: the ID prefix is `HPA-` (Hook Point Action), not `OA-` or `ORC-`.

- **Django model:** `cbhooks.HookPointAction` at `cbhooks/models.py:3168`.
- **Serializer:** `OrchestrationActionSerializer` at `cbhooks/api/v3/serializers/hook_point_action.py:17`.
- **Top-level sync target?** Yes.
- **Standalone or linked?** Always standalone — the hook_point binding determines when it fires; it doesn't belong to a blueprint.
- **Colocated files:** `<HPA-id>_metadata.json` + `<HPA-id>_script.py` + optional shared-module deps. No custom-form path.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `name` | string (≤255) | `href` title + import match key. |
| `hook_point` | string | The HookPoint label; resolved via `HookPoint.objects.get(label=...)`. |
| `enabled` | boolean | Default `false`. |
| `run_on_statuses` | string | Default `""`. |
| `continue_on_failure` | boolean | Default `false`. |
| `minimum_version_required` | string | Default `"8.6"`. |

### Optional fields

`id` (`HPA-<id>`), `last_updated`, `description`, `run_seq` (integer/null; auto-assigned if null), `maximum_version_required`, `base_action_name`, `action_inputs` (kebab-case), `action_inputs_sequence`, `action_input_default_values`, `icon`, `dependencies`.

HPA has no `label`, `dangerous`, `requires_approval`, or button-mixin fields.

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| `hook_point` | `cbhooks.HookPoint` (by `label` string) | Hard requirement; unresolvable label → import falls back to v2 serializer. |
| `dependencies.hook` | `plugins/OHK-*` | |
| `dependencies.sharedModules[]` | `shared_modules/SHM-*` | camelCase. |

### `hook_point` enum (HookPoint labels)

HookPoints are DB rows, not a code enum. The **seeded set** below comes from `initialize/cb_minimal.py`. An install may add/rename. Emit exactly what `str(hook_point)` produces — match is by exact label, including trailing spaces where present.

`Pre Sync VMs`, `Post Sync VMs`, `Pre Server Refresh`, `Pre-Provision`, `Pre-Create Resource`, `Pre-Provisioning Engine Server Creation`, `Pre-Power-on`, `Pre-Network Configuration`, `During Resource Provisioning`, `Post-Network Configuration`, `Network Verification`, `Post-Network Verification`, `Pre-Application Installation`, `Ansible Automation Configuration`, `Post-Provision`, `Pre-Install Applications`, `Post-Install Applications`, `Pre-Uninstall Applications`, `Post-Uninstall Applications`, `Pre-Delete`, `Post-Delete`, `Pre-Server Modification`, `Post-Server Modification`, `Pre-Expire Server`, `Post-Expire Server`, `Generated Hostname Overwrite`, `Validate Hostname`, `Pre-CIT`, `Post-CIT`, `Blueprint Action`, `Pre-Job`, `Post-Job`, `Post Group Creation`, `Pre Group Modification`, `Post Group Modification`, `Post Environment Creation`, `Pre Environment Modification`, `Post Environment Modification`, `Pre Power On`, `Post Power On`, `Pre Power Off`, `Post Power Off`, `Pre Reboot`, `Post Reboot`, `Order Submitted for Approval`, `Order Submitted for Approval Notifications`, `Server Tier Validation`, `Blueprint Validation`, `Generated Resource Name Overwrite`, `Post-Order Denied`, `Post-Order Approval`, `Post-Order Execution`, `Post-Order Execution Notification`, `Waiting for Config Manager Agent Checkin`, `Parameter Change`, `SSO User Update`, `External Users Sync`, `Generated Parameter Options`, `Server Actions`, `Resource Actions`, `Pre-Delete Resource`, `Post-Delete Resource`, `Validate IP Address`, `Compute Server Rate`, `Validate Order Recipient`, `Terraform Pre-Provision`, `Terraform Init`, `Terraform Plan`, `Terraform Apply`, `Terraform Post-Provision ` (trailing space), `Terraform Provision Failure ` (trailing space), `Terraform Provision Cleanup ` (trailing space), `Terraform Pre-Destroy ` (trailing space), `Terraform Destroy`, `Terraform Destroy Failure`, `Terraform Post-Destroy ` (trailing space), `Terraform Destroy Cleanup`.

### Worked example

```json
{
    "id": "HPA-1234abcd",
    "type": "CloudBolt Plug-in",
    "base_action_name": "Tag Newly Provisioned Server",
    "name": "Tag Newly Provisioned Server",
    "hook_point": "Post-Provision",
    "enabled": true,
    "run_seq": 1,
    "run_on_statuses": "",
    "continue_on_failure": false,
    "description": "Applies cost-center tags after provisioning",
    "minimum_version_required": "8.6",
    "maximum_version_required": "",
    "script_filename": "HPA-1234abcd_script.py",
    "last_updated": "2026-05-29",
    "action_inputs": [],
    "action_input_default_values": [],
    "dependencies": {"hook": "plugins/OHK-xxxxxxxx"}
}
```

---

## 6. `flowcontrol_actions/` — FCA-*

- **Django model:** `cbhooks.FlowControlAction` at `cbhooks/models.py:6544`.
- **Serializer:** `FlowControlActionSerializer` at `cbhooks/api/v3/serializers/flowcontrol_actions.py:18`.
- **Top-level sync target?** Yes.
- **Standalone or linked?** Standalone — FCAs are gates that run on filter matches, not blueprint children.
- **Colocated files:** `<FCA-id>_metadata.json` + `<FCA-id>_script.py` + optional shared-module deps. No custom-form path.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `label` | string (≤255) | The only serializer-required field; unique; import match key. |

### Optional fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | string | — | `FCA-<id>`. |
| `control_type` | string | `"ALLOW"` (UI form default) | Discriminator — see enum. Reapplied on import. |
| `description` | string / null | `null` | |
| `enabled` | boolean | `false` | Not in serializer required set. |
| `last_updated`, `base_action_name`, `minimum_version_required`, `maximum_version_required`, `action_inputs` (kebab-case), `action_input_default_values`, `icon`, `dependencies` | — | — | Inherited shared fields. |

FCA's M2M filter relations (groups, blueprints, environments, resource_technologies, resource_handlers, resource_types) are **not** in the v3 serializer and are not exported/imported.

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| `dependencies.hook` | `plugins/OHK-*` | The auto-created flow-control plugin. |
| `dependencies.sharedModules[]` | `shared_modules/SHM-*` | camelCase. |

### `control_type` enum

**`ALLOW` | `PAUSE`** (stored uppercase) — `cbhooks/forms.py:2206-2209`, default `"ALLOW"` (`forms.py:2215`).

> ⚠️ Not `ALLOW`/`DENY`. The semantics are inverted when `control_type != "ALLOW"`: `ALLOW` lets matching orders proceed; `PAUSE` blocks/pauses (`cbhooks/models.py:6745`). The model field is a free `CharField` with no DB choices — the enum is enforced only by the form, not the serializer/model — so a hand-edited file with a bogus value will import without error.

### Worked example

```json
{
    "id": "FCA-5e6f7a8b",
    "type": "CloudBolt Plug-in",
    "base_action_name": "Block Prod Orders During Freeze",
    "label": "Block Prod Orders During Freeze",
    "control_type": "PAUSE",
    "description": "Pauses orders that match the change-freeze filters",
    "enabled": true,
    "minimum_version_required": "8.6",
    "maximum_version_required": "",
    "script_filename": "FCA-5e6f7a8b_script.py",
    "last_updated": "2026-05-29",
    "action_inputs": [],
    "action_input_default_values": [],
    "dependencies": {"hook": "plugins/OHK-xxxxxxxx"}
}
```

---

## 7. `recurring_jobs/` — RJB-*

- **Django model:** `cbhooks.RecurringActionJob` at `cbhooks/models.py:5891` (subclass of `jobs.RecurringJob` at `jobs/models.py:2308`).
- **Serializer:** `RecurringActionJobSerializer` at `cbhooks/api/v3/serializers/recurring_action_job.py:18`.
- **Top-level sync target?** Yes.
- **Standalone or linked?** Always standalone — RJBs run on schedule, not on blueprint events.
- **Colocated files:** None. The runnable code lives on the base hook referenced via `dependencies.hook`.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | `RJB-<id>`. |
| `name` | string | Unique; import match key. |
| `type` | string | Job-type slug (see enum). |
| `schedule` | string | 5-field cron string. |
| `create_date` | string (ISO datetime) | Model default `datetime.now`. |
| `enabled` | boolean | Default `false`. |
| `allow_parallel_jobs` | boolean | Default `false`. |

### Optional fields

`description` (default `""`), `last_run` (ISO datetime/null; not consumed on import), `last_updated`, `minimum_version_required` (`"8.6"`), `maximum_version_required` (`""`), `base_action_name`, `action_inputs` (kebab-case), `action_input_default_values` (`[]`), `has_custom_form`, `icon`, `dependencies`.

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| `dependencies.hook` | `plugins/OHK-*` | Always. |
| `dependencies.custom_form` | `forms/FRM-*` | If set. |

### Enums

- `type` — a `Job.JOB_TYPES` slug (`jobs/models.py:806-945`). For recurring hooks the relevant values are `action` and `orchestration_hook`.
- `schedule` — standard 5-field cron string (`min hour dom month dow`), max length 255. Validated at runtime by `croniter` / `pycron`.

### Worked example

```json
{
    "id": "RJB-a1b2c3d4",
    "name": "Nightly Server Sync",
    "description": "Runs the sync plug-in every night",
    "type": "action",
    "schedule": "0 2 * * *",
    "create_date": "2026-05-01T12:00:00",
    "last_run": null,
    "enabled": true,
    "allow_parallel_jobs": false,
    "base_action_name": "Nightly Sync Plug-in",
    "minimum_version_required": "8.6",
    "maximum_version_required": "",
    "last_updated": "2026-05-29",
    "action_input_default_values": [],
    "dependencies": {"hook": "plugins/OHK-z9y8x7w6"}
}
```

---

## 8. `cit_tests/` — CIT-*

- **Django model:** `cscv.ActionCITTest` at `cscv/models.py:179` (MTI subclass of `cscv.CITTest` at `cscv/models.py:68`).
- **Serializer:** `ActionCITSerializer` at `cscv/api/v3/serializers/action_cit.py:32`.
- **Top-level sync target?** Yes.
- **Standalone or linked?** Standalone.
- **Colocated files:** None. Executable script lives on the base hook via `dependencies.hook`.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | `CIT-<id>`. |
| `name` | string (≤255) | Import match key. |
| `dependencies` | object | Must contain `hook`. |
| `base_action_name` | string | Name of the underlying hook. |

### Optional fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `expected_status` | string | `"SUCCESS"` | Model choices `SUCCESS`/`WARNING`/`FAILURE`; create-API also accepts `ERROR`. Import does no enum validation. |
| `expected_output` | string | `""` | Substring match; blank matches any. |
| `enabled` | boolean | `true` | Set by base-action flow, not by CIT override. |
| `notes` | string | `""` | |
| `max_retries` | integer | `0` | |
| `timeout_limit` | integer / null | `0` on import | |
| `labels` | array[string] | `[]` | Cleared and re-added on import. |
| `action_inputs` (kebab-case), `action_input_default_values` | array | — | Shared schema. |
| `last_updated`, `minimum_version_required`, `maximum_version_required` | — | — | |

`last_status`, `last_duration`, and `last_retry_count` are excluded from export.

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| `dependencies.hook` | `plugins/OHK-*` | Load-bearing — import fails without the hook folder present. |

### Worked example

```json
{
    "id": "CIT-ab12cd34",
    "name": "Provision VM smoke test",
    "expected_status": "SUCCESS",
    "expected_output": "",
    "enabled": true,
    "notes": "",
    "max_retries": 0,
    "timeout_limit": 0,
    "labels": ["smoke"],
    "action_inputs": [],
    "action_input_default_values": [],
    "base_action_name": "Run VM Provision",
    "minimum_version_required": "",
    "maximum_version_required": "",
    "last_updated": "2026-05-29",
    "dependencies": {"hook": "plugins/OHK-xxxxxxxx"}
}
```

---

## 9. `webhooks/` — IWH-*

- **Django model:** `cbhooks.InboundWebHook` at `cbhooks/models.py:3007`.
- **Serializer:** `InboundWebHookSerializer` at `cbhooks/api/v3/serializers/inbound_web_hook.py:21`.
- **Top-level sync target?** Yes.
- **Standalone or linked?** Standalone.
- **Colocated files:** None. Executable code lives on the base hook via `dependencies.hook`.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | `IWH-<id>`. |
| `label` | string | Required; unique. |

### Optional fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `uri_path` | string | `global_id` if empty | Unique URL path; format rules below. |
| `authentication_method` | string | `"basic"` (model default) | See enum below. |
| `token` | string | `secrets.token_urlsafe` | Export-only and conditional — emitted only when `exporting=True` AND `authentication_method == "token"`. |
| `description`, `enabled`, `minimum/maximum_version_required`, `last_updated`, `base_action_name`, `action_inputs` (kebab-case), `action_input_default_values`, `dependencies` | — | — | Inherited shared fields. |

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| `dependencies.hook` | `plugins/OHK-*` | The webhook's runnable code. |
| `dependencies.custom_form` | `forms/FRM-*` | If set. |

### Enums

- `authentication_method` (`AUTH_METHOD_CHOICES`, `cbhooks/models.py:3023`): `"normal"` (Default CB API Auth), `"token"` (Token-based). The model's literal default `"basic"` is **outside** this set (legacy holdover; treat as equivalent to `"normal"` for new authoring).
- `uri_path` format (`cbhooks/validators.py:11`): no leading/trailing `/`; only `[A-Za-z0-9_\-/]`; must NOT match the Global ID format. Empty defaults to `global_id` at save time.

HTTP methods are not stored — the IWH dispatches at runtime to `inbound_web_hook_<method>` on the base hook.

### Runtime contract (verified against CloudBolt source)

- **URL:** `GET|POST /api/v3/cmp/inboundWebHooks/<uri_path>/run/` — trailing slash required (a GET without it is a 301). Lookup is by `uri_path` only; the global ID works because `uri_path` defaults to it. Only GET and POST are allowed.
- **Entry point:** `InboundWebHook.run_hook` calls `inbound_web_hook_<method>` on the base plugin with kwargs `parameters`, `files`, `profile`, `job` (always `None`) and `logger`; kwargs are filtered by the function signature. Canonical signature: `def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs)`.
- **Input:** GET → `parameters` is `request.GET` (a QueryDict; values are strings, `.getlist()` for repeats). POST → `parameters` is `request.data` (JSON/form/multipart, native types, no CamelCase conversion). The DRF request is **not** passed. Query-string names `filter` and `last` are reserved by the API layer and `;` must be avoided. `action_inputs` are irrelevant to IWH calls — nothing maps them.
- **Output:** the return value is JSON-rendered as-is (dict/list/str/number). For a non-200 status return `{"iwh_status_code": N, "iwh_embedded_response": <body>}` (other keys are dropped; an invalid code becomes 500). An uncaught exception is a 500 whose body includes `str(exc)`.
- **Auth:** `normal` → session, CloudBolt token or JWT; requires `request.user.is_authenticated`; CSRF applies to POST only, so a same-origin GET from a custom form works with the session cookie. `token` → `?token=` (or a `token` body field), no user, `profile is None`. **No RBAC either way** — see [rbac-and-security.md](rbac-and-security.md#inbound-webhooks-do-no-rbac-of-their-own).
- **Cost:** runs synchronously in the web process; no Job, ActionHistory or event row per call; plugin source cached 30 s; only the global user rate throttle applies.
- **Template-rendered:** like every plugin, the script is rendered through the template engine first — never write literal `{{ }}` in it (cardinal rule 3).

### Round-trip caveats

- `token` is export-only and only present when `authentication_method == "token"`. Import sets it straight from the metadata and does **not** regenerate it: a token-mode IWH with no `token` in the metadata imports with an empty token, and an empty `?token=` then passes the check. Prefer `"normal"`; if you must use token mode, keep the token in the metadata or re-save the IWH in the UI after sync.
- `uri_path` is made unique on a clash (`name_000X`), so a form that hardcodes the path can silently point at the wrong hook if two repos ship the same path.

### Worked example

```json
{
    "id": "IWH-1a2b3c4d",
    "label": "Provision Trigger Webhook",
    "uri_path": "provision-trigger",
    "authentication_method": "token",
    "token": "<redacted>",
    "description": "Inbound webhook that kicks off a provision",
    "enabled": true,
    "base_action_name": "Provision Trigger Hook",
    "minimum_version_required": "8.6",
    "maximum_version_required": "",
    "last_updated": "2026-05-29",
    "action_input_default_values": [],
    "dependencies": {"hook": "plugins/OHK-9z8y7x6w"}
}
```

For `authentication_method == "normal"`, `token` is absent.

---

## 10. `shared_modules/` — SHM-*

- **Django model:** `cbhooks.SharedModule` at `cbhooks/models.py:4589`.
- **Serializer:** `SharedModuleSerializer` at `cbhooks/api/v3/serializers/shared_module.py:9` (thin subclass of `BaseActionSerializer`).
- **Top-level sync target?** Yes.
- **Colocated files:** `<SHM-id>_metadata.json` + `<SHM-id>_script.py` (named by `script_filename`). The module body comes from `module_file` / `file_content()`.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | `SHM-<id>`. |
| `type` | string | Must equal `"Shared Module"` (case-insensitive on import). |
| `name` | string | Unique across `OrchestrationHook`; auto-derived from `module_name`. |
| `module_name` | string | Python import name; unique; drives `from shared_modules.<module_name> import ...`. |
| `script_filename` | string | `<id>_script.py`. Required on import. |

### Optional fields

`label`, `description` (default `""`), `shared` (default `true`), `max_retries` (`0`), `source_code_url` (when URL-sourced; then `script_filename` is the URL basename), `minimum/maximum_version_required`, `last_updated`, `target_os_families`, `resource_technologies`, `action_inputs` (snake_case; normally absent — `SharedModule` no-ops input updates), `action_inputs_sequence`, `dependencies`, `icon`.

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| `dependencies.sharedModules[]` | `shared_modules/SHM-*` (other modules) | **camelCase.** Recursively exported/imported. |

### Validation

- `type` must equal `"Shared Module"`.
- `module_name`: valid Python module name, unique, max 100 chars (`validate_module_name` in `cbhooks/validators.py`).
- Shared modules have no HTTP/auth/schedule enums (they are not runnable).

### Worked example

```json
{
    "id": "SHM-7f6e5d4c",
    "type": "Shared Module",
    "name": "common_helpers",
    "label": "Common Helpers",
    "module_name": "common_helpers",
    "description": "Reusable helper functions for plug-ins",
    "shared": true,
    "max_retries": 0,
    "script_filename": "SHM-7f6e5d4c_script.py",
    "minimum_version_required": "8.6",
    "maximum_version_required": "",
    "last_updated": "2026-05-29",
    "dependencies": {"sharedModules": ["shared_modules/SHM-0a1b2c3d"]}
}
```

---

## 11. `extensions/` — XUI-*

- **Django model:** `extensions.UIExtension` at `extensions/models.py:26`.
- **Serializer:** `UIExtensionSerializer` at `extensions/api/v3/serializers.py:60`.
- **Top-level sync target?** Yes.
- **Colocated files:** A whole **package directory tree**, not a single script. Alongside `<XUI-id>_metadata.json`:
  1. A subfolder named after metadata `name`, containing every package file at its relative path.
  2. The icon file (named by metadata `icon`).
  The file list is the metadata key `package_contents` (array of relative path strings). On import, files are read from `<local_repo_path>/<name>/<relative_path>` and copied into `proserv/xui/<name>/`.
- **Default extensions exported:** `.py`, `.html`, `.png`, `.svg`, `.jpg`, `.js`, `.css`, `.json`, `.yaml`, `.yml`, `.rst`, `.md`, `.xml` (overridable via `ALLOWED_XUI_EXTENSIONS`). `.pyc` filtered out.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | `XUI-<id>`; duplicate-rejected on import. |
| `name` | string | Lowercase + underscores, unique; becomes the package subfolder and install dir. |
| `package_contents` | array[string] | Relative paths of every package file. |

### Optional fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `label` | string | `name` | |
| `description` | string / null | `null` | |
| `enabled` | boolean | `true` | Set on import only if truthy. |
| `version` | string | `""` | |
| `icon` | string | omitted | Colocated icon filename. |
| `minimum_version_required` | string | `"8.6"` | |
| `maximum_version_required` | string | `""` | |
| `last_updated` | date | export date | |

### Cross-reference fields

**None.** `UIExtension` has no dependency linkage; the flattened exporter always returns `{}`.

### Worked example

```json
{
    "id": "XUI-mvsj4az6",
    "name": "sample_report_extensions",
    "label": "Sample Report Extensions",
    "description": "Sample dashboard reports",
    "enabled": true,
    "version": "",
    "icon": "sample_report_extensions.jpg",
    "minimum_version_required": "8.6",
    "maximum_version_required": "",
    "last_updated": "2026-05-29",
    "package_contents": [
        "__init__.py", "forms.py", "views.py",
        "reports/bar.html", "reports/pie.html",
        "reports/logged_in_users.html", "reports/login_users_table.html", "reports/table.html"
    ]
}
```

On disk this sits next to a `sample_report_extensions/` folder + `sample_report_extensions.jpg`.

> **Note:** `extensions/models.py` also defines `CbApplet` (`XUIC-`), `Applet` (`APL-`), and `AppletComponent` (`APLC-`) with their own serializer, but **only `UIExtension` is registered for content export**. CbApplets are NOT a Source Control Repos content type.

---

## 12. `forms/` — FRM-* (transitive-import only)

> ⚠️ **Export-only at top level.** `forms/` appears in the export registry (`model_repo_mapping`) but NOT in the sync registry (`OBJECT_TYPE_MAPPINGS`). Forms are imported **only as transitive dependencies** of a parent blueprint or action. A `forms/FRM-*/` folder not referenced anywhere will never import — it just sits on disk.

- **Django model:** `customforms.CustomForm` at `customforms/models.py:13`.
- **Serializer:** `CustomFormSerializer` at `customforms/api/v3/serializers/custom_forms.py:35`.
- **Top-level sync target?** No — transitive import only.
- **Colocated files:** Optional `<FRM-id>_css.css` (named by metadata `css_file`, basename only). Form functions are NOT inlined — referenced via `dependencies.form_functions`.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | `FRM-<id>`. |
| `json` | string | SurveyJS form definition as a **serialized JSON string** — NOT a literal JSON object. The blueprint importer's `update_custom_form_mapping_ids` (`servicecatalog/api/v3/serializers/service_blueprint_import_export_mixin.py`) calls `json.loads()` on this value to rewrite the embedded `custom_form_id`/`blueprint_id`/`BDI-` ids for the target instance, so a literal object fails the import with `TypeError: the JSON object must be str, bytes or bytearray, not dict`. Store it as `json.dumps(survey)`. |

### Optional fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `rendering_mode` | string | `"vue"` on import (model default `"jquery"`) | `jquery` / `vue`. |
| `supported_languages` | string | `""` | Comma-delimited; JQueryUI mode only. |
| `css_file` | string | omitted | Basename of colocated CSS. |
| `functions` | array[string] | omitted | Function metadata filenames (populated on import from resolved deps). |
| `dependencies` | object | omitted | `{"form_functions": [...]}`. |
| `created` / `modified` | datetime | model timestamps | Not consumed on import. |

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| `dependencies.form_functions[]` | `form_functions/FJS-*` | snake_case key. Each function is recursively exported. |
| (implicit) parent | blueprint or action | Not in metadata — the parent's `dependencies.custom_form` points here. |

### Enums

- `rendering_mode`: `jquery` | `vue`.

### Worked example

```json
{
    "id": "FRM-ab12cd34",
    "json": "{\"pages\": []}",
    "rendering_mode": "vue",
    "supported_languages": "",
    "created": "2026-01-15T10:00:00Z",
    "modified": "2026-05-01T12:30:00Z",
    "css_file": "FRM-ab12cd34_css.css",
    "dependencies": {"form_functions": ["form_functions/FJS-99887766"]}
}
```

---

## 13. `form_functions/` — FJS-* (transitive-import only)

> ⚠️ **Export-only at top level.** Same constraint as `forms/`: imported only as a transitive dependency of a parent `CustomForm`. The function's `<FJS-id>_metadata.json` is scanned when its parent form imports.

- **Django model:** `customforms.CustomFormFunction` at `customforms/models.py:101`.
- **Serializer:** `CustomFormFunctionSerializer` at `customforms/api/v3/serializers/form_functions.py:20`.
- **Top-level sync target?** No — transitive only.
- **Colocated files:** None. JS is inline in metadata `code` field.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | `FJS-<id>`. |
| `name` | string | Unique, max 255. |
| `code` | string | The complete JS function (model `function_code`). |

### Optional fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `description` | string / null | `""` on import | |
| `asynchronous` | boolean | `false` on import | |

### Cross-reference fields

| Field path | Target | Notes |
|---|---|---|
| (reverse) parent forms | `customforms.CustomForm` (M2M) | Not in metadata; the link is set from the form side via `function_obj.custom_forms.add(custom_form)`. |

### Worked example

```json
{
    "id": "FJS-99887766",
    "name": "validateHostname",
    "description": "Checks hostname against RFC-1123",
    "code": "function validateHostname(value) { /* ... */ }",
    "asynchronous": false
}
```

---

## Navigation Cookbooks

### Cookbook 1 — Given a blueprint, find its build/teardown/discovery plugins, day-2 actions, attached form

Open `blueprints/BP-<id>/BP-<id>_metadata.json` and follow:

| You want | Field path | Target |
|---|---|---|
| Build plugin(s) | `deployment_items[].dependencies.hook` | `plugins/OHK-*` |
| Teardown plugin(s) | `teardown_items[].dependencies.hook` | `plugins/OHK-*` |
| Discovery plugin | `discovery_plugin.dependencies.hook` | `plugins/OHK-*` |
| Day-2 actions | `management_actions[].dependencies.resource_action` | `resource_actions/RSA-*` (each → its `dependencies.hook` → the plugin) |
| Attached form | `dependencies.custom_form` | `forms/FRM-*` |
| Sub-blueprints | `deployment_items[].dependencies.blueprint` | `blueprints/BP-*` (recursive) |

### Cookbook 2 — Given a plugin, find every content unit that references it

Grep across all `<GLOBAL_ID>_metadata.json` files for the plugin's path string `"plugins/OHK-<id>"`:

```bash
grep -rl '"plugins/OHK-<id>"' --include='*_metadata.json' .
```

A reference may appear in any of: `dependencies.hook`, `dependencies.displayCondition`, `deployment_items[].dependencies.hook`, `teardown_items[].dependencies.hook`, `discovery_plugin.dependencies.hook`, `deployment_items[].dependencies.rate_hook`, `deployment_items[].dependencies.environment_selection_hook`, `parameters[].gen_options_hooks[].dependencies.*`.

A plugin with zero matches is an **orphan** — possibly a shared utility intentionally not yet referenced, possibly dead.

### Cookbook 3 — Given a standalone content unit (e.g. a recurring job), find its plugin

Open the unit's metadata file and read `dependencies.hook`. The value is a path like `"plugins/OHK-<id>"`. The Python entry point is at `<that-path>/OHK-<id>_script.py`; the entry-point function name is determined by the plugin's `type` field (e.g. `run(job, **kwargs)` for `"CloudBolt Plug-in"`).

For `cit_tests/CIT-*`, `recurring_jobs/RJB-*`, `webhooks/IWH-*`, the unit itself has no `script_filename` — all runnable code lives on the referenced plugin.

### Cookbook 4 — Given a human-readable name, find the content unit

Grep across all `<GLOBAL_ID>_metadata.json` files for `name`, `label`, or `description`:

```bash
grep -rli '"name": "Expire Servers"' --include='*_metadata.json' .
# → recurring_jobs/RJB-nsx4v2s1/RJB-nsx4v2s1_metadata.json
```

Different content types use different match keys (`name` for BP/OHK/HPA/RJB/CIT/SHM/XUI/FRM/FJS, `label` for RSA/SVA/FCA/IWH), so search both.

---

## Extending this schema

If you encounter a CloudBolt content type that is not in this document:

1. Verify it is a real Source Control Repos type by checking the `model_repo_mapping` dict in `source_code_repos/models.py:1093` of the CloudBolt source.
2. If present, find its serializer in `*/api/v3/serializers/`. The serializer's `Meta.fields`, `create_required_fields`, and `*_from_local_path` methods together define the schema.
3. Update this file from the serializer: amend the affected sections and add a section for any new content type.
4. Confirm whether the type is a top-level sync target by checking `OBJECT_TYPE_MAPPINGS` in `source_code_repos/services.py:44`. If absent, it is transitive-import only (like `forms/` and `form_functions/`) and must be referenced from a parent to ever sync in.
