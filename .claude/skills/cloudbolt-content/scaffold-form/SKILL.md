---
name: scaffold-form
description: Scaffold a CloudBolt custom form (forms/FRM-*) plus optional form_functions/FJS-* JavaScript helpers. Wires the form into a named parent content unit (blueprint or action) via dependencies.custom_form — without that wiring, the form will never sync into CloudBolt.
when-to-use: When the user wants to add a custom order form to a blueprint, resource action, server action, recurring job, or inbound webhook. Trigger on "add a custom form to X", "create a SurveyJS form for X", "scaffold a form".
---

# scaffold-form

Generate `forms/FRM-<id>/` with stub SurveyJS metadata, optionally with `form_functions/FJS-<id>/` JS helpers, AND wire the form into a named parent so it actually imports.

## Required reading

Load [docs/agents/metadata-schemas.md](../../../docs/agents/metadata-schemas.md): §0 (universal rules), §12 (forms), §13 (form_functions). **Read the transitive-import warning in §12/§13 before anything else** — it is load-bearing for this skill.

## Why the parent-wiring matters

`forms/` and `form_functions/` are **export-only at the top level**. They appear in CloudBolt's export registry but NOT in the sync registry. The importer picks them up **only as transitive dependencies** of a parent (blueprint or action's `dependencies.custom_form`; form's `dependencies.form_functions[]`). An orphan form sits on disk forever.

This skill enforces parent attachment.

## When to use

- The user wants a SurveyJS form on a blueprint, resource action, server action, recurring job, or inbound webhook.
- The user wants to add custom JS validation/transformation functions to an existing form.

## Inputs

Ask the user:

1. **Form purpose / name** (for the description).
2. **Parent content unit** (required). A blueprint folder (e.g. `blueprints/BP-5pei9cno`) or an action folder (RSA / SVA / HPA / FCA / RJB / IWH). If the user can't name one, abort: "A form must be referenced from a parent to import; tell me which content unit owns this form, or use `find-content-by-name` to locate it."
3. **Rendering mode** (default `"vue"`; alt `"jquery"`). See §12 Enums.
4. **Whether to include CSS** (default no). If yes, scaffolds `<FRM-id>_css.css` as an empty file.
5. **How many form_functions** to scaffold alongside (default 0). For each, ask the function's `name` (e.g. `"validateHostname"`).

## Procedure

1. **Verify the parent exists.** Glob for the parent folder; refuse if missing.

2. **Generate global_ids** per §0 ID format. Random-shell-out, **never hand-typed**:

   ```bash
   python -c "import random,string,sys; a=string.ascii_lowercase+string.digits; [print(p+'-'+''.join(random.choices(a,k=8))) for p in sys.argv[1:]]" FRM FJS
   ```

   (Pass one `FJS` arg per form function.) Grep for collisions; regenerate on duplicate.

3. **Emit `forms/FRM-<id>/FRM-<id>_metadata.json`** matching §12 Required Fields, using §12 Worked Example as the structural template. The `json` field gets an empty SurveyJS schema `{"pages": []}` — do NOT pre-populate; SurveyJS schema is at https://surveyjs.io/Documentation/Library and counts as an external vendor surface per [external-apis.md](../../../docs/agents/external-apis.md). If CSS requested, add `"css_file": "FRM-<id>_css.css"` and create the empty CSS file alongside. If form_functions requested, populate `dependencies.form_functions[]` with their paths per §0 cross-reference convention.

4. **Emit each `form_functions/FJS-<id>/FJS-<id>_metadata.json`** per §13 Required Fields. The `code` field gets a `function <name>(value) { /* TODO */ }` stub — do NOT pre-write business logic.

5. **Wire the form into the parent.** Edit the parent's `<GLOBAL_ID>_metadata.json` and merge into its `dependencies` block. If `dependencies.custom_form` already exists, **abort** and tell the user the parent already has a form attached — they can remove the existing reference first or use that form. For blueprints specifically, also set the top-level `custom_form` field carrying the bare global_id (not the path) and `has_custom_form: true`. Exact cross-reference shape per §0 cross-reference convention.

6. **Report back** with:
   - The form folder (and CSS / function folders if any).
   - The parent metadata file you modified and the exact `dependencies.custom_form` line you added.
   - Reminder that `json` is an empty SurveyJS schema — the agent's next step is to fill it; consult SurveyJS docs per [external-apis.md](../../../docs/agents/external-apis.md), do NOT guess the schema.
   - **Pinned defaults must be mirrored.** A custom form bypasses the parent deployment item's `parameter_defaults`: the plugin receives only what the form submits. When filling the schema, add a hidden text question for every pinned input — `{"type": "text", "name": "plugin-bdi-<item>.<input>", "visible": false, "defaultValue": "<the BDI value>", "isRequired": true}` — and tell the user both copies must stay identical. Dropdowns for declared inputs use `parameterOptions`; fields inside a Dynamic Panel need an inbound webhook (`webhooks/IWH-yj93is5z`). See [plugin-templates.md → Custom forms and pinned defaults](../../../docs/agents/plugin-templates.md#custom-forms-and-pinned-defaults).
   - For each FJS scaffolded: the `code` field is a stub.
   - Suggestion to run `validate-metadata`.

## Constraints

- IDs random per §0 (step 2), never hand-typed.
- The form MUST be referenced from at least one parent before completion (per §12 transitive-only rule). Orphan forms never sync.
- Refuse to overwrite an existing form (`forms/FRM-<id>/` exists) — regenerate the ID.
- The SurveyJS schema in `json` is intentionally empty in the stub; do not pre-populate guessed pages/elements.
- When the schema is later filled, every BDI `parameter_default` of the parent must appear as a hidden `defaultValue` question with the same value (custom forms do not receive BDI defaults).
- `form_functions[]` `code` field is the complete function body as a string; do NOT pre-write business logic.
