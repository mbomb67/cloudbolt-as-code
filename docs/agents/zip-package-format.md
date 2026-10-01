# CloudBolt zip package format

How CloudBolt's **Import** (UI upload or `POST /api/v3/cmp/<type>/` with `zipFile`) expects content to be packaged, and how that differs from this repo's Source Control Repos layout. `tools/package_zip.py` implements this conversion; the `cb-package-zip` skill drives it.

Derived from the CloudBolt source at v2026.3.67 (serializer paths cited as `app/file.py:line`) and checked against real exports. Re-derive with [zip-package-format-source-query.md](zip-package-format-source-query.md) when CloudBolt changes.

## 1. The package shape

Every exportable object is one **package**: a zip holding a folder `<slug>/` with `<slug>.json` (the metadata) and sibling files. `slug` is Django `slugify(name)` with hyphens turned to underscores (`utilities/cb_http.py:183-226`): `"AD DNS - Create A Record"` → `ad_dns_create_a_record`. Dependencies are **nested zips** inside that folder, each a package of its own.

```
ad_dns_create_a_record.zip                       # an Orchestration Action (HPA)
└── ad_dns_create_a_record/
    ├── ad_dns_create_a_record.json              # HPA metadata: id "HPA-…", name, hook_point, …
    └── ad_dns_create_a_record.zip               # its plugin (OHK), a nested package
        └── ad_dns_create_a_record/
            ├── ad_dns_create_a_record.json      # OHK metadata: id "OHK-…", type, script_filename, shared_module_dependencies
            ├── OHK-dnsadd01_script.py           # named exactly as script_filename
            └── shared_module_ldap_dns.zip       # one per shared module
                └── ldap_dns/
                    ├── ldap_dns.json            # SHM metadata: id "SHM-…", type "Shared Module", module_name, script_filename
                    └── ldap_dns.py
```

How the importer reads a layer (`api/v3/mixins.py:318-366`) decides everything else:

- **Every `.json` member is "the" metadata; the last one wins.** One JSON per zip layer, nested zips are opaque. A loose README.json or a plugin script named `*.json` breaks the import.
- **Every other member goes into a dict keyed by basename.** Folder names are cosmetic; two members with the same basename collide.
- **`id` must start with the destination type's prefix** (`BP-`, `OHK-`, `RSA-`, …, `api/v3/mixins.py:199-281`). Missing `id` is rejected as a legacy (v2) zip.
- `shared_module_dependencies: ["shared_module_<slug>.zip", …]` on any package imports those members as Shared Modules first (`api/v3/mixins.py:153-166`).
- Uncompressed size per layer ≤ 2 GB.

## 2. Repo layout → package, per type

Universal edits when converting a `<ID>_metadata.json` from this repo:

| Repo key | In the zip |
|---|---|
| `dependencies` (at any depth) | **Remove.** Each reference becomes a nested zip (table below). The importer ignores the key, but the nested zip is what it actually reads. |
| `metadata_version` | **Remove.** If present, the blueprint importer treats the zip as the flattened format and silently skips every nested zip (`service_blueprint_import_export_mixin.py:1425,1692-1710`). |
| `has_custom_form` | Remove (informational). |
| everything else | Keep verbatim. Real exports add `base_action_name`, `last_updated`, `minimum_version_required` ("8.6" default), `maximum_version_required`; none are required. |

| Repo content | Package slug | Members besides `<slug>.json` | Reference in the JSON |
|---|---|---|---|
| `plugins/OHK-*` | slug(`name`) | the script (basename = `script_filename`); `shared_module_<slug>.zip` per `dependencies.sharedModules[]`; `genoptions_<slug>.zip` per `action_inputs[].gen_options_hooks[].dependencies.orchestration_hook` | `script_filename`; `shared_module_dependencies[]` = those member names |
| `shared_modules/SHM-*` | slug(`module_name`) | `<module_name>.py` (CloudBolt relocates it to `shared_modules/<module_name>.py` on save) | `script_filename` = `<module_name>.py`, `type` = `"Shared Module"` |
| `resource_actions/RSA-*`, `server_actions/SVA-*`, `webhooks/IWH-*`, `mcp_tool_actions/MTA-*`, `flowcontrol_actions/FCA-*` | slug(`label`) | **first non-JSON member = the plugin zip** (`<hook slug>.zip`); SVA: `condition_<slug>.zip` for `dependencies.displayCondition`; RSA/SVA: `custom_form_<slug>.zip` for `dependencies.custom_form` | none: the plugin zip is found by position (`action_base.py:736`), the condition and form by name prefix |
| `orchestration_actions/HPA-*`, `recurring_jobs/RJB-*` | slug(`name`) | the plugin zip, first | HPA `hook_point` must be an existing hook-point label; RJB `type` must be `"orchestration_hook"` |
| `cit_tests/CIT-*` | — | **No zip import exists** (no UI dialog, no API route). Sync from the repo. | |
| `forms/FRM-*` | `custom_form_<slug(parent name)>` | the CSS file (basename = `css_file`); `form_function_fjs_<suffix>.zip` per `dependencies.form_functions[]` | `functions[]` = those member names; `json` must stay a **string**. Forms import only nested inside their parent. |
| `form_functions/FJS-*` | slug(`FJS-id`) → `fjs_<suffix>` | none (the JavaScript is the `code` key) | `id`, `name`, `code` |
| `extensions/XUI-*` | slug(`name`) | `<name>.zip` whose members are `<name>/<path>` for every `package_contents[]` file; optional icon | `name` locates the inner zip; `package_contents` is used only when there is no inner zip. `enabled: false` is ignored on import. |
| `blueprints/BP-*` | slug(`name`) | see §3 | |

Member names must be unique within a layer; keep the plugin zip the first non-JSON member of an action package (the importer takes `list(temp_files)[0]`), and do not write directory entries.

## 3. Blueprint packages

The blueprint JSON keeps `deployment_items`, `teardown_items`, `management_actions`, `parameters`, `discovery_plugin`, `resource_type`, `labels`, `icon` as exported. Nested content is matched by **member-name prefix**, not by any JSON key (`service_item_mixin.py:491-562`, `service_blueprint_import_export_mixin.py:1764-1801`):

| Repo reference | Nested member | Notes |
|---|---|---|
| `deployment_items[i].dependencies.hook` | `build_<deploy_seq>_<plugin slug>.zip` | one per item; two items with the same `deploy_seq` collide (last wins) |
| `teardown_items[i].dependencies.hook` | `teardown_<deploy_seq>_<plugin slug>.zip` | `deploy_seq` is usually negative |
| `deployment_items[i].dependencies.blueprint` | `build_<deploy_seq>_<blueprint slug>.zip` (a full blueprint package) | matched on `build_<seq>` **without** the underscore, so seq 1 also matches `build_10_…`; renumber if needed |
| `deployment_items[i].dependencies.rate_hook` | `ratehook_<deploy_seq>_<slug>.zip` | item must carry `rate_action_name` |
| `deployment_items[i].dependencies.environment_selection_hook` | `environment_selection_<deploy_seq>_<slug>.zip` | item must carry `environment_selection_orchestration: {"title": <plugin name>}` |
| `discovery_plugin.dependencies.hook` | `discovery_<slug>.zip` | |
| `management_actions[i].dependencies.resource_action` | `management_<RSA label slug>.zip` (an RSA package, §2) | resource actions not in the zip are detached on replace |
| `parameters[i].gen_options_hooks[j].dependencies.orchestration_hook` | `genoptions_<HPA slug>.zip` (an HPA package) | `gen_options_hooks[j].name` must be a prefix of the HPA `name`, else the import raises; an entry with no action must be dropped |
| `dependencies.custom_form` | `custom_form_<slug>.zip` (a form package, §2) | the `custom_form`/`has_custom_form` keys are not consulted |
| `icon` | the image file, same basename | |
| `deployment_items[i].dependencies.jobengine_selection_hook` | none | lost on zip import; re-select in the UI |
| `tfconfig`, `tfoperation`, `terraform`, `pod`, `loadbalancer`, `network` items | Terraform working directories / provider objects | not carried by the repo layout; export those blueprints from CloudBolt |

Server tiers need no nested zip. Their `os_build.title`, environments and groups are matched **by name** on the target and silently skipped when absent.

## 4. What does not survive the trip

- **Redaction placeholders** (`YOUR_CREDENTIALS`, `YOUR_AUTH_INFO`, `YOUR_EMAIL_INFO`) are skipped by the importer; the real values must be entered in CloudBolt afterwards.
- **Password-type defaults** (`PWD`/`ETXT` inputs in `action_input_default_values` or `parameter_defaults`) are ciphertext bound to the exporting instance; they decrypt only with that instance's key or the export password.
- `action_input_default_values[].name` and `parameter_defaults[].name` are matched to inputs by the prefix before the last `_` segment, so they must end in `_a<digits>` (CloudBolt rewrites the number). A bare `env_id` would be matched as `env`.
- `action_inputs[].value_pattern_string` is overwritten by `formatter_pattern` on import; ship both.
- `enabled` on RJB, and the whole `allow_parallel_jobs`, are ignored; RSA `new_tab_url` and MTA `is_synchronous` are exported but not imported.
- Groups, environments, OS builds, hook points, resource types, and MCP tool names are resolved by name; an MTA whose `mcp_tool_name` already exists fails the import.
- **A blueprint that already exists on the target as a Source Control Repos sync cannot be replaced by zip.** With `replaceExisting`, the importer matches the existing blueprint by global ID and, because its `remote_source_url` is set, switches to the repo-refresh code path: it looks for `Deployment Item <seq> <name>/…` folders (then `<slug>/<slug>.json`) under the cloned repo on the appliance instead of reading the nested zips, and fails with `Failed to fetch remote file for action … from URL 'file:///var/opt/cloudbolt/repos/…'` (`service_item_mixin.py` `hydrate_action`, `if item.blueprint.remote_source_url`; same for management actions and generated-options actions). Import without replace (CloudBolt creates "<name> (2)" with a fresh global ID), remove the synced copy first, or just sync the repo. Actions have no such branch.

## 5. Where the zip goes

| Type | Upload |
|---|---|
| Blueprint | Blueprints list → Upload; `POST /api/v3/cmp/blueprints/` |
| Orchestration action | Admin → Orchestration Actions → Upload; `POST /api/v3/cmp/orchestrationActions/` |
| Resource / Server action | Admin → Resource Actions / Server Actions → Upload; `POST /api/v3/cmp/resourceActions/`, `…/serverActions/` |
| Recurring job | Admin → Recurring Jobs → Upload; `POST /api/v3/cmp/scheduledActions/` |
| Inbound webhook | Admin → Inbound Web Hooks → Upload; `POST /api/v3/cmp/inboundWebHooks/` |
| MCP tool action | Admin → MCP Tool Actions → Upload; `POST /api/v3/cmp/mcpToolActions/` |
| Flow control action | API only: `POST /api/v3/cmp/flowControlActions/` |
| Plugin on its own | API only: `POST /api/v3/cmp/actions/` (the UI has no bare-plugin upload) |
| Shared module on its own | UI only: Admin → Shared Modules → Upload |
| UI extension | Admin → UI Extensions → Upload; `POST /api/v3/cmp/uiExtensions/` (restart the web server afterwards) |
| CIT test, form, form function | no standalone import |

API uploads are multipart: `zipFile=@file.zip`, optional `replaceExisting=true` (match by global ID, then name, and update in place; otherwise a copy named "… (2)" is created), `ignoreActionEnabled=true`, `password=` for encrypted defaults.
