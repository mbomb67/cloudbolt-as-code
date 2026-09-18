---
name: scaffold-standalone-action
description: Scaffold any standalone CloudBolt action — resource_action (RSA), server_action (SVA), orchestration_action (HPA), flowcontrol_action (FCA), recurring_job (RJB), cit_test (CIT), or inbound_webhook (IWH) — paired with its plugin (OHK). Produces a runnable folder set with valid stub metadata wired via dependencies.hook.
when-to-use: When the user wants to add a content unit that does NOT belong to a blueprint — e.g. "add a recurring job that cleans up X nightly," "add a server action to deploy the migration agent," "add a hook-point action that tags servers after provisioning," "add a flow control gate that blocks Friday prod orders," "add a CIT test for the provision flow," "add an inbound webhook that triggers Y."
---

# scaffold-standalone-action

Generate the folder set for any standalone CloudBolt action content type plus its paired `plugins/OHK-<id>/` folder. Wires them via `dependencies.hook = "plugins/OHK-<id>"`.

For blueprints, use `scaffold-blueprint` instead. For custom forms (FRM/FJS), use `scaffold-form` — those are transitive-import only.

## Required reading

Load [docs/agents/metadata-schemas.md](../../../docs/agents/metadata-schemas.md). Use §0 (universal rules) and §2 (plugins — every action type pairs with an OHK plugin) plus the section that matches the chosen action type:

| Action type input | metadata-schemas.md section | Top-level dir |
|---|---|---|
| `resource_action` | §3 | `resource_actions/RSA-*` |
| `server_action` | §4 | `server_actions/SVA-*` |
| `orchestration_action` | §5 | `orchestration_actions/HPA-*` |
| `flowcontrol_action` | §6 | `flowcontrol_actions/FCA-*` |
| `recurring_job` | §7 | `recurring_jobs/RJB-*` |
| `cit_test` | §8 | `cit_tests/CIT-*` |
| `inbound_webhook` | §9 | `webhooks/IWH-*` |

## Inputs

Ask the user:

1. **Action type** (single select from the seven above).
2. **Human-readable name** (required). Example: `"Nightly Server Sync"` for an RJB, `"Tag Newly Provisioned Server"` for an HPA.
3. **Type-specific user-facing decisions** — prompt only for the chosen type. These are user-facing choices, not defaults; the full required-fields list lives in the matching schema section.
   - **`recurring_job`**: `schedule` (cron string, default `"0 2 * * *"`).
   - **`orchestration_action`**: `hook_point` — the HookPoint label. Common defaults: `"Post-Provision"`, `"Pre-Delete"`, `"Generated Hostname Overwrite"`. The full seeded list is in §5 Enums. Labels are case-sensitive and some carry trailing whitespace — preserve exactly what §5 documents.
   - **`flowcontrol_action`**: `control_type` — `"ALLOW"` or `"PAUSE"` (NOT `"DENY"`; default `"ALLOW"`).
   - **`cit_test`**: `expected_status` — `"SUCCESS"`, `"WARNING"`, or `"FAILURE"` (default `"SUCCESS"`); `expected_output` (default empty string).
   - **`inbound_webhook`**: `authentication_method` — `"normal"` or `"token"` (default `"normal"`); `uri_path` (default = generated `global_id`).
   - **`server_action`**: optional `dialog_title_template` (default `"Run {{action.label}} on {{server.hostname}}"`).
   - **`resource_action`**: no type-specific extras beyond standard ButtonActionMixin defaults.

## Procedure

1. **Generate two global_ids** per §0 ID format — one for the action's content folder (its prefix per the table above) and one for the paired `plugins/OHK-<id>/`. Random-shell-out, **never hand-typed** (an LLM-authored "random" suffix encodes semantics and is obviously not CloudBolt-generated):

   ```bash
   python -c "import random,string,sys; a=string.ascii_lowercase+string.digits; [print(p+'-'+''.join(random.choices(a,k=8))) for p in sys.argv[1:]]" RSA OHK
   ```

   Substitute the action's prefix for `RSA`. Add `CF` args if you're scaffolding action_inputs. Grep for collisions; regenerate on duplicate.

2. **Emit `plugins/OHK-<id>/`** per §2 Required Fields: `OHK-<id>_metadata.json` (`type: "CloudBolt Plug-in"`, `name: "<action name>"`, `script_filename: "OHK-<id>_script.py"`) and `OHK-<id>_script.py` stub with `def run(job, **kwargs):` and a docstring naming the action type and any expected Action Inputs.

3. **Emit the action content folder** at `<top-level-dir>/<PREFIX>-<id>/<PREFIX>-<id>_metadata.json` matching the chosen type's section in metadata-schemas.md. Use that section's Worked Example as the structural template; substitute user-provided inputs into the fields they chose. Universal rules from §0 that bear on this output:
   - `action_inputs[]` keys are **kebab-case** for every type in this skill's scope (`HasBaseActionSerializer` lineage — see §0 casing rule).
   - `dependencies.hook` is always `"plugins/OHK-<plugin-id>"` (snake_case key, path-string value per §0 cross-reference convention).
   - Optional camelCase fields when applicable: `dependencies.displayCondition` (SVA only), `dependencies.sharedModules[]`. Snake_case `dependencies.custom_form` applies to most action types — only set if the user is also adding a form (use `scaffold-form` for that).

4. **Report back** with:
   - The two folders created and the cross-reference wired.
   - The user-facing choice that landed in the file (cron `schedule` for RJB, `hook_point` label for HPA, ALLOW vs PAUSE for FCA, `expected_status` for CIT, `authentication_method` for IWH).
   - Reminder that the plugin script is a stub — agent's next step is the Python body.
   - If the plugin will call a third-party API, point at [docs/agents/external-apis.md](../../../docs/agents/external-apis.md).
   - Suggestion to run `validate-metadata`.

## Constraints

- IDs random per §0 (step 1), never hand-typed.
- For FCA: `control_type` is `"ALLOW"` or `"PAUSE"` only, never `"DENY"` (common LLM-memory hallucination; CloudBolt's source enforces ALLOW/PAUSE per §6 Enums).
- For HPA: `hook_point` must match a HookPoint label exactly per §5 (including trailing whitespace where present in source).
- For RJB: `schedule` must be a valid 5-field cron string.
- `action_inputs[]` keys are kebab-case for every type in this skill (per §0). The paired OHK plugin's `action_inputs[]` use snake_case (per §0).
- Do not pre-write business logic in the plugin script beyond the entry-point signature and a docstring placeholder.
