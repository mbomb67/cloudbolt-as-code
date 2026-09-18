---
name: scaffold-blueprint
description: Scaffold a new CloudBolt blueprint — generates a blueprints/BP-<id>/ folder plus paired plugins/OHK-<id>/ folders for build/teardown/discovery plugins, with valid stub metadata wired via dependencies.hook cross-references.
when-to-use: When the user says "scaffold a blueprint", "create a new blueprint", "add a blueprint for X", or wants to start a new orderable service in this CloudBolt content repo. Use as a faster alternative to hand-authoring the BP-/OHK- folder pair and their metadata wiring.
---

# scaffold-blueprint

Generate the folder set for a new CloudBolt blueprint and its paired plugins. Produces a runnable scaffold — does NOT pre-write CloudBolt plugin Python beyond a docstring stub and the canonical `run(job, **kwargs)` signature; the agent fills the body.

## Required reading

Load [docs/agents/metadata-schemas.md](../../../docs/agents/metadata-schemas.md): §0 (universal rules), §1 (blueprints), §2 (plugins), §3 (resource_actions). Those sections are the authoritative schemas; this skill is the procedure that generates content matching them.

## When to use

- The user wants to add a new orderable resource (e.g. "Azure VM," "AWS Lambda Function," "ServiceNow Incident") and you're starting from scratch.
- Use as the first step when authoring any blueprint — even one that will only have build + teardown.

## Inputs

Ask the user:

1. **Blueprint name** (human-readable, required). Example: `"AWS S3 Bucket"`.
2. **Resource type label** (singular and plural, required for `resource_type`). Example: singular `"S3 Bucket"`, plural `"S3 Buckets"`.
3. **Which plugins to scaffold** (multi-select). Defaults: Build (recommended yes), Teardown (recommended yes — idempotent reverse), Discovery (yes if the blueprint maps to discoverable cloud resources).
4. **How many day-2 resource actions** to scaffold alongside (default 0).
5. **Provider hint** (optional — `"aws"`, `"azure"`, `"servicenow"`, etc.) — informs the docstring stub but not metadata.

## Procedure

1. **Generate global_ids** per §0 ID format. For every ID you need — `BP-` (1), `OHK-` (one per plugin), `RSA-` (one per day-2), nested `BDI-` (one per `deployment_items[]` / `teardown_items[]`), nested `CF-` (one per action input) — generate the suffix randomly. **Do not hand-type the suffix** — an LLM-authored "random" string encodes semantics (`OHK-s3bld001`, `BDI-teardown1`) and is obviously not CloudBolt-generated. Shell out:

   ```bash
   python -c "import random,string,sys; a=string.ascii_lowercase+string.digits; [print(p+'-'+''.join(random.choices(a,k=8))) for p in sys.argv[1:]]" BP OHK OHK OHK RSA OHK BDI BDI
   ```

   Adjust the prefix list to match what you need. Grep the repo and regenerate on any collision (CloudBolt does not enforce uniqueness — see §0).

2. **Emit `blueprints/BP-<id>/BP-<id>_metadata.json`** matching §1 Required Fields, using §1 Worked Example as the structural template. Substitute user inputs for placeholders. Load-bearing details worth restating because their absence breaks import:
   - **`"metadata_version": "2026.1.0"` is REQUIRED.** Without it, CloudBolt silently skips the blueprint on import. Mirror whatever value other blueprints in the repo carry if it has drifted.
   - `deployment_items[].dependencies.hook = "plugins/OHK-<build-id>"`; same shape for `teardown_items[]` and `discovery_plugin` per §1 Cross-reference fields and §0 cross-reference convention.
   - `management_actions[].dependencies.resource_action = "resource_actions/RSA-<id>"` for each day-2 the user requested.
   - Default `deployment_items[].tier_type` is `"plugin"`. Other tier_types (`provserver`, `terraform`, `tfconfig`, `tfoperation`) require richer sub-schemas — see §1 Enums — and should only be used if the user explicitly asks.
   - Omit `discovery_plugin` entirely if the user said "no discovery."

   **Truly optional export-only fields — fine to omit when scaffolding.** Blueprints exported from a live CloudBolt instance also carry `resource_name_template` and `resource_type.id` (`RT-<id>`) + `resource_type.internal_only`. CloudBolt assigns or derives them on sync, so a hand-scaffolded blueprint imports fine without them. Include them only when editing an already-exported blueprint that has them, or when round-trip-faithful output is requested.

3. **Emit one `plugins/OHK-<id>/` per selected plugin type.** Each contains:
   - `OHK-<id>_metadata.json` matching §2 Required Fields (`type: "CloudBolt Plug-in"`, `script_filename`, etc.).
   - `OHK-<id>_script.py` stub with the entry point for that plugin role:
     - **Build:** `def run(job, **kwargs):` returning a 3-tuple. Docstring names expected Action Inputs.
     - **Teardown:** same shape; docstring notes the `WARNING`-on-missing-resource idempotency rule.
     - **Discovery:** `RESOURCE_IDENTIFIER` module variable + `def discover_resources(**kwargs):`. Docstring notes that all relevant handler types must be enumerated.

   Full Python templates: [docs/agents/plugin-templates.md](../../../docs/agents/plugin-templates.md).

4. **Emit day-2 resource actions** (if any) at `resource_actions/RSA-<id>/`, matching §3 Required Fields. Wire each into the blueprint's `management_actions[]` per §1 Cross-reference fields. Note `action_inputs[]` on RSAs use kebab-case keys per §0 casing rule.

5. **Report back** with:
   - Every folder created.
   - Every cross-reference wired.
   - Reminder that plugin scripts are docstring stubs — the agent's next step is to fill the Python body.
   - If plugins will call third-party APIs, point at [docs/agents/external-apis.md](../../../docs/agents/external-apis.md).
   - Suggestion to run `validate-metadata` to confirm the scaffold is clean.

## Constraints

- IDs must match §0 ID format, generated randomly via the shell-out in step 1, never hand-typed.
- Do not overwrite existing folders; regenerate the ID on collision.
- Do not pre-write plugin business logic beyond the entry-point signature and a docstring placeholder.
- Top-level dir names assume defaults per AGENTS.md. Only probe for `SourceCodeRepo.path_to_*` overrides if a default dir is missing.
