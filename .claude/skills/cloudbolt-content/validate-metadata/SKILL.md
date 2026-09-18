---
name: validate-metadata
description: Lint every <GLOBAL_ID>_metadata.json file in a CloudBolt Source Control Repos content repo. Reports missing required fields, dangling cross-references, ID/folder mismatches, enum violations, key-casing mismatches in action_inputs[], orphan transitive content, and secret-placeholder values.
when-to-use: When the user says "validate my metadata", "lint the content", "check the repo for errors", "validate this folder", or before committing/pushing changes to a content repo. Also use after running any scaffold-* skill to confirm the generated files are clean.
---

# validate-metadata

Structural lint pass over every `<GLOBAL_ID>_metadata.json` file in the repo.

This is a **best-effort linter**, not a full semantic validator. CloudBolt itself ships no JSON Schema or Pydantic model — full validation requires round-tripping through CloudBolt's DRF serializers. This skill catches structural mistakes that block sync.

## Required reading

Load [docs/agents/metadata-schemas.md](../../../docs/agents/metadata-schemas.md). This skill validates content against the schemas there. Specifically:

- **§0** for universal rules: ID format, cross-reference convention, `action_inputs[]` casing, secret redaction placeholders, transitive-import asymmetry.
- **§1–§13** for per-content-type Required Fields tables, Cross-reference fields, Enums, and Round-trip caveats.

When CloudBolt changes a schema, the linter inherits the change automatically — `metadata-schemas.md` is the single source of truth.

## When to use

- Pre-commit / pre-push validation.
- After running any `scaffold-*` skill.
- When a sync is failing and you suspect a metadata problem.
- Periodic health checks on a content repo.

## Inputs

1. **Scope** (default: whole repo). User may pass a single folder (e.g. `blueprints/BP-5pei9cno`) to scope.
2. **Severity threshold** (default: `info`). User may pass `error` to suppress warnings/info.

## Procedure

1. **Discover content folders.** For each top-level content-type directory listed in AGENTS.md that exists in the repo, list its `<PREFIX>-*/` subfolders and locate each one's `<GLOBAL_ID>_metadata.json`. Missing top-level dirs → skip silently (the repo simply has no content of that type).

2. **For each metadata file, run the checks below.** Severity per check is given. Report findings grouped by file at the end.

### Check A — JSON well-formedness + folder/ID consistency
- **Error:** file fails JSON parse.
- **Error:** metadata `id` field does not match the folder name.
- **Error:** filename does not match `<folder-name>_metadata.json`.
- **Error:** ID does not match the regex in §0 ID format.

### Check B — Required fields per content type
Look up the file's content type and verify every field listed in its **Required Fields** table is present.
- **Error:** missing any required field.
- **Error (especially loud):** blueprint missing `metadata_version`. Without this field CloudBolt silently skips the blueprint on import — call this out prominently in the report.

### Check C — Colocated file presence
- **Error:** `script_filename` declared but the file is absent (plugins, shared modules, RSA, SVA, HPA, FCA).
- **Error:** files in XUI `package_contents` don't exist in the nested package directory.
- **Warning:** files exist in the XUI package directory but are NOT in `package_contents`.

### Check D — Cross-reference resolution
Per §0 cross-reference convention:
- **Error (bad shape):** value is not a string of the form `"<top-level-dir>/<PREFIX>-<8-12 chars>"`. Bare IDs, numeric IDs, or embedded objects are invalid.
- **Error (dangling):** the referenced folder does not exist on disk.
- **Error (wrong target type):** e.g., `dependencies.hook` points to `blueprints/BP-*` instead of `plugins/OHK-*`.
- **Warning (custom dir prefix):** the path uses a non-default top-level dir name. Only flag if the reference doesn't resolve (the repo may have customized `SourceCodeRepo.path_to_*`).

### Check E — Enum violations
For every field listed in the matching section's **Enums** subsection, verify the value is in the allowed set. Common cases:
- FCA `control_type` ∈ {`ALLOW`, `PAUSE`} (§6 — NOT `DENY`).
- CIT `expected_status` ∈ {`SUCCESS`, `WARNING`, `FAILURE`, `ERROR`} (§8).
- IWH `authentication_method` ∈ {`normal`, `token`, `basic`} (§9 — `basic` is a legacy model default but acceptable).
- Plugin `type` discriminator (§2).
- Blueprint `deployment_items[].tier_type` (§1).
- HPA `hook_point` — **warning only** (DB-resident, may include customer additions; §5).

### Check F — action_inputs[] key casing
Per §0 casing rule:
- **Error:** snake_case-lineage content type (plugins, shared_modules) has an `action_inputs[]` item using kebab-case keys.
- **Error:** kebab-case-lineage content type (RSA, SVA, HPA, FCA, RJB, CIT, IWH) has an `action_inputs[]` item using snake_case keys.

### Check G — Orphan transitive content
- **Warning:** `forms/FRM-*` folder is not referenced by any parent's `dependencies.custom_form` (§12 — will not sync).
- **Warning:** `form_functions/FJS-*` folder is not referenced by any form's `dependencies.form_functions[]` (§13 — will not sync).
- **Info:** `plugins/OHK-*` folder is not referenced by any other content unit. May be intentional (shared utility) or dead.

### Check H — Secret placeholders
Per §0 round-trip section:
- **Info:** field value equals a redaction placeholder (`"YOUR_CREDENTIALS"`, `"YOUR_AUTH_INFO"`, `"YOUR_EMAIL_INFO"`, `"SOURCE_CODE_URL Redacted"`). Indicates a sync happened and the secret must be re-entered in the running CloudBolt UI; the repo itself is fine.

### Check I — Parameter dependency integrity
Per §0 [Parameter dependencies](../../../docs/agents/metadata-schemas.md#parameter-dependencies-field_dependency__set). For each `action_inputs[]` item's `field_dependency_controlling_set` / `field_dependency_dependent_set` entries (resolve names by stripping the `_a<hookid>` suffix to match `action_inputs[].name`):
- **Error (dangling field ref):** a `controlling-field.name` or `dependent-field.name` does not match any `action_inputs[].name` in the same file.
- **Error (malformed suffix):** a `controlling-field.name` / `dependent-field.name` does not end in `_a<digits>` (e.g. `_a8r1` rather than `_a357`). On import CloudBolt rewrites `_a\d+$ → _a{action.id}` then matches `CustomField.name` exactly (`FieldDependencySerializer.get_or_create_from_dict`); a non-digit suffix fails the regex, is left unrewritten, matches nothing, and the dependency is **silently dropped** (no error), so the dependent field's generator never receives `control_value`/`control_value_dict`. Each ref must be `_a` + digits (the value is arbitrary — overwritten on import — and the two mirrored copies need not share the same integer). Live signature: the dependent dropdown never refreshes, and a round-trip export shows the `field_dependency_*_set` arrays empty.
- **Error (un-mirrored):** a relationship in field C's `field_dependency_dependent_set` has no identical twin in field D's `field_dependency_controlling_set` (or vice versa). Every dependency must appear on **both** ends.
- **Error (enum):** `dependency-type` ∉ {`REGENOPTIONS`, `SHOWHIDE`, `HIDE`}.
- **Error (code/metadata mismatch):** a field has a `REGENOPTIONS` controller but the colocated script has no `generate_options_for_<name>` method — or that method reads `control_value`/`control_value_dict` but the field declares **no** controllers (the values will always be empty). Match the generator's arity to the controller count per §0.
- **Warning (sequence):** in `action_inputs_sequence`, a dependent field is ordered before one of its controllers.

3. **Report** as a grouped summary:
   ```
   N errors, M warnings, K info findings across F files.

   ERRORS:
     blueprints/BP-abc12345/BP-abc12345_metadata.json
       - Check B: missing required field "metadata_version" (load-bearing — CloudBolt will silently skip this blueprint on import).
       - Check D (dangling): dependencies.hook = "plugins/OHK-nonexistent" — folder does not exist.

     ...

   WARNINGS:
     forms/FRM-orphan01/FRM-orphan01_metadata.json
       - Check G: not referenced by any parent's dependencies.custom_form. Will not sync.

     ...

   INFO:
     plugins/OHK-shared01/OHK-shared01_metadata.json
       - Check H: webhook auth_header_value = "YOUR_AUTH_INFO" — re-enter in CloudBolt UI after sync.
   ```

4. **Exit conditions:** zero errors = pass. Any error = fail. Warnings and info don't fail the check but should be surfaced.

## Constraints

- Read-only — never modify files.
- Default to canonical top-level dir names; skip absent dirs silently.
- The schemas in `metadata-schemas.md` may lag CloudBolt itself if customers run a newer CMP. If a "missing required field" error doesn't make sense, suggest the user check the field against their CloudBolt version and update `docs/agents/metadata-schemas.md`.
- Do NOT silently fix issues. Report and let the agent or user decide. If the user explicitly asks "fix these errors," make targeted edits — never bulk-rewrite.
