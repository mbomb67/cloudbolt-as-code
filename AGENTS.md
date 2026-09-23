# AGENTS.md — CloudBolt Source Control Repos

This repo holds CloudBolt CMP content (blueprints, plugins, resource actions, XUI extensions, etc.) that a running CloudBolt instance syncs via its **Source Control Repos** feature. The repo is the source of truth.

## Layout

Content lives in **ID-prefixed folders** under type-specific top-level dirs, each carrying a colocated `<GLOBAL_ID>_metadata.json`. Cross-references between content live in the JSON, not in directory structure or filenames.

| Dir | Prefix | What it holds |
|---|---|---|
| `blueprints/` | `BP-` | Orderable resources |
| `plugins/` | `OHK-` | Python scripts (8 subtypes; `type` field discriminates) |
| `resource_actions/` | `RSA-` | Day-2 resource actions |
| `server_actions/` | `SVA-` | Day-2 server actions |
| `orchestration_actions/` | `HPA-` | Hook-point lifecycle actions |
| `flowcontrol_actions/` | `FCA-` | ALLOW/PAUSE gates |
| `recurring_jobs/` | `RJB-` | Cron-scheduled jobs |
| `cit_tests/` | `CIT-` | Integration tests |
| `webhooks/` | `IWH-` | Inbound REST endpoints |
| `shared_modules/` | `SHM-` | Reusable Python libraries |
| `extensions/` | `XUI-` | Django UI extensions |
| `forms/` | `FRM-` | Custom forms (transitive-import only) |
| `form_functions/` | `FJS-` | Form JS helpers (transitive-import only) |
| `typings/` | n/a | CloudBolt API type stubs, copied from your appliance and gitignored (see [docs/dev-environment-setup.md](docs/dev-environment-setup.md)) |

Full per-type schemas, enums, required fields, and worked examples: **[docs/agents/metadata-schemas.md](docs/agents/metadata-schemas.md)**.

## Cardinal rules

1. **Read the metadata to navigate.** Filesystem layout does not tell you which plugin a blueprint uses. The relationships are in the JSON. To find what calls what, open the relevant `<GLOBAL_ID>_metadata.json` and follow its `dependencies` fields.
2. **Cross-references are repo-relative path strings** of the form `"<top_level_dir>/<GLOBAL_ID>"` (e.g. `"plugins/OHK-abcd1234"`). Never bare IDs, numeric IDs, or embedded objects. Key casing varies — see the table in [metadata-schemas.md §0](docs/agents/metadata-schemas.md#0-universal-rules).
3. **`{{ }}` is for declared plugin inputs only — nowhere else, and always quoted.** CloudBolt renders the *entire* plugin script through its template engine before the Python runs — comments and docstrings included — so any `{{ … }}` anywhere is treated as a variable to resolve and will corrupt or break the script. Only write `{{ }}` to inject a parameter you actually declared as a plugin input; in comments and prose, name the variable in plain words, never with literal `{{ }}` braces. When you do inject one, always quote/cast it: `api_key = "{{ api_key }}"`, never `api_key = {{ api_key }}` (unquoted = Python injection); `int("{{ port }}")`; lists/dicts via `ast.literal_eval("""{{ items }}""")`. Full rules: [docs/agents/rbac-and-security.md](docs/agents/rbac-and-security.md).
4. **Never expose `resource_handler` to end users.** Always gate via Environments (`env_id`); convert to handler inside `run()` with `rh = env.resource_handler.cast()`. Required for RBAC compliance. **Every Environment dropdown is built from `group.get_available_environments()`** — the platform's entitlement query, which returns the environments explicitly entitled to the requesting group (and its ancestors) *plus* all unconstrained environments (no groups assigned). Users must be able to order into both, so never reimplement entitlement with `Environment.objects.filter(group__in=[group])` — it silently drops unconstrained environments. Resolve the `group` kwarg first (it arrives as a `Group` or as its name depending on caller), then narrow by handler type with `id__in`. See [docs/agents/rbac-and-security.md](docs/agents/rbac-and-security.md).
5. **Never guess external vendor APIs.** When code calls Azure ARM, AWS, ServiceNow, Okta, or any third-party service, reference the vendor's current official docs at the call site — do not extrapolate from memory. Cite the doc URL in a code comment. If the operation is already in `typings/` or wrapped by an existing `shared_modules/SHM-*`, that's the source of truth. Full rule: [docs/agents/external-apis.md](docs/agents/external-apis.md).
6. **Generate global IDs with CloudBolt's algorithm — never hand-type the suffix.** A suffix like `OHK-dnsadd01` encodes a name/type/sequence and is immediately identifiable as not CloudBolt-generated; a real `global_id` suffix is 8 characters of pure entropy. Shell out one ID per prefix you need — `python -c "import random,string,sys; a=string.ascii_lowercase+string.digits; [print(p+'-'+''.join(random.choices(a,k=8))) for p in sys.argv[1:]]" OHK BP RSA` — then grep the repo and regenerate on any collision. This holds whether or not you go through a `scaffold-*` skill. Full rule: [metadata-schemas.md §0](docs/agents/metadata-schemas.md#0-universal-rules).

## Standalone vs. linked content

Resource/server/orchestration/flowcontrol actions, recurring jobs, CIT tests, and inbound webhooks may live **standalone** — no parent blueprint required. The absence of a parent reference is normal.

`forms/` and `form_functions/` are different: CloudBolt exports them as top-level folders but imports them **only as transitive dependencies** of a parent (via `dependencies.custom_form` or `dependencies.form_functions[]`). An orphan form will never sync.

**A custom form replaces the native order form completely.** When a blueprint has a custom form, the deployment item's `parameter_defaults` are **not** applied to the plugin's inputs — the plugin receives only what the form submits. Every pinned per-blueprint value (connection, repo, branch, module ID, …) must therefore also exist in the form as a hidden text question `plugin-bdi-<item>.<input>` with a `defaultValue` equal to the BDI default, and the two copies must stay identical. Fields inside a Dynamic Panel are not plugin inputs, so their dropdowns cannot use `generate_options_for_*`; use `parameterOptions` for declared inputs and an inbound webhook for panel fields. Details: [plugin-templates.md → Custom forms and pinned defaults](docs/agents/plugin-templates.md#custom-forms-and-pinned-defaults).

## Round-trip is not lossless

When CloudBolt exports content to a repo, secrets are redacted to placeholder strings (`"YOUR_CREDENTIALS"`, `"YOUR_AUTH_INFO"`, `"YOUR_EMAIL_INFO"`, `"SOURCE_CODE_URL Redacted"`) and the importer skips them on re-sync. Customers must re-enter secrets after every sync. Instance-specific bindings (groups, environments) are also dropped. Full caveats: [metadata-schemas.md](docs/agents/metadata-schemas.md).

## Top-level dir names

The dir names above are CloudBolt's defaults. They CAN be customized per repo via `SourceCodeRepo.path_to_*` (shell + API call), but the override is rare. **Assume the defaults.** Only probe for customization if a default dir is missing or a cross-reference path doesn't resolve.

## Tooling

Six Claude Code skills under `.claude/skills/cloudbolt-content/` automate the common workflows. Portable across tools that support skills.

| Skill | What it does |
|---|---|
| `scaffold-blueprint` | Generate BP + paired OHK plugins (build/teardown/discovery) + optional RSAs |
| `scaffold-standalone-action` | Generate any of RSA/SVA/HPA/FCA/RJB/CIT/IWH + paired OHK plugin |
| `scaffold-form` | Generate FRM (+ optional FJS) wired into a named parent so it actually imports |
| `scaffold-bicep` | Generate a BP that deploys an Azure Bicep template on the generic Bicep engine (see blueprints/BP-nibk4erf) |
| `validate-metadata` | Lint every `<GLOBAL_ID>_metadata.json` for required fields, dangling refs, enum violations, key casing, orphans |
| `find-content-by-name` | Resolve human-readable name to ID-prefixed folder |

`tools/build_catalog.py` regenerates `CATALOG.md`, `catalog.json`, and every top-level dir's index `README.md` from the metadata. Run it after adding, renaming, or re-describing content; never hand-edit those files (CI fails if they are stale). Keep every metadata `description` to one sentence stating what the content does, because the catalog prints it verbatim.

## Deep references — load on demand

- **[docs/agents/metadata-schemas.md](docs/agents/metadata-schemas.md)** — every content type's required/optional fields, enums, cross-reference tables, navigation cookbooks, worked examples. Authoritative.
- **[docs/agents/plugin-templates.md](docs/agents/plugin-templates.md)** — Python templates for build/discovery/teardown/day-2/XUI; entry points, return formats, action context kwargs.
- **[docs/agents/common-patterns.md](docs/agents/common-patterns.md)** — Azure auth, AWS paginators, discovery hydration, generate-options patterns, error handling.
- **[docs/agents/rbac-and-security.md](docs/agents/rbac-and-security.md)** — full RBAC pattern, parameter-quoting rules, secret handling.
- **[docs/agents/external-apis.md](docs/agents/external-apis.md)** — full "never guess vendor APIs" rule with Azure REST and AWS boto3 worked examples.
- **`typings/`** — CloudBolt's internal Django models as type stubs. Grep here for available methods and fields, e.g. `grep -r "def cast" typings/`. It is gitignored; copy it from `/var/opt/cloudbolt/proserv/typings` on your appliance: [docs/dev-environment-setup.md](docs/dev-environment-setup.md). If `typings/` is missing, copy it before writing code against CloudBolt APIs; do not guess them from memory.

## External CloudBolt documentation

- [Plugin structure](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/structure-of-a-plug-in), [parameterization](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/plug-in-parameterization)
- [Action types](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/action-types), [action context](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/action-context)
- [CloudBolt Forge](https://github.com/CloudBoltSoftware/cloudbolt-forge) — community-maintained example actions and blueprints
