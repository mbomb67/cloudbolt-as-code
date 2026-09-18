---
name: find-content-by-name
description: Resolve a human-readable string ("Expire Servers", "Azure Cross-Tenant Subscription") to the ID-prefixed folder containing the matching CloudBolt content. Useful because on-disk folders are named by GLOBAL_ID (e.g. BP-5pei9cno), not by name.
when-to-use: When the user asks "where is the X blueprint?", "find the plugin for Y", "open the recurring job named Z", or any task that starts from a human-readable content name rather than a GLOBAL_ID. Also use internally from other skills (scaffold-form, etc.) when the user names a parent by label rather than ID.
---

# find-content-by-name

Grep every `<GLOBAL_ID>_metadata.json` file in the repo for a name/label match and return the matching folder paths. Different content types use different match keys, so search several.

## Required reading

Load [docs/agents/metadata-schemas.md](../../../docs/agents/metadata-schemas.md) when you need to look up which fields constitute a content type's human-readable identifier — §1–§13 Required Fields tables list `name` vs `label` per type. The skill's own table below is a quick reference, but the schema doc is authoritative.

## When to use

- User mentions a content unit by its display name and you need to locate the folder.
- Other skills need to resolve a parent reference from a name (e.g. `scaffold-form` asks "which parent?" and the user names it).

## Inputs

1. **Query string** (required). The substring to match against name/label/description fields. Default: case-insensitive substring match.
2. **Content type filter** (optional). One or more of: `blueprint`, `plugin`, `resource_action`, `server_action`, `orchestration_action`, `flowcontrol_action`, `recurring_job`, `cit_test`, `webhook`, `shared_module`, `extension`, `form`, `form_function`. Default: all types.
3. **Exact-match flag** (optional, default `false`).

## Procedure

1. **Determine fields to search per content type.** Different types use different match keys (per [docs/agents/metadata-schemas.md](../../../docs/agents/metadata-schemas.md) Required Fields):

| Content type | Primary match field | Secondary fields |
|---|---|---|
| Blueprint (`BP-`) | `name` | `description`, `resource_type.label`, `resource_type.plural_label` |
| Plugin (`OHK-`) | `name` | `description` |
| Resource action (`RSA-`) | `label` | `description` |
| Server action (`SVA-`) | `label` | `description` |
| Orchestration action (`HPA-`) | `name` | `description` |
| Flow control action (`FCA-`) | `label` | `description` |
| Recurring job (`RJB-`) | `name` | `description` |
| CIT test (`CIT-`) | `name` | `notes` |
| Inbound webhook (`IWH-`) | `label` | `description` |
| Shared module (`SHM-`) | `name` | `module_name`, `label`, `description` |
| XUI extension (`XUI-`) | `name` | `label`, `description` |
| Form (`FRM-`) | none (forms have no human-readable name field) | n/a |
| Form function (`FJS-`) | `name` | `description` |

2. **Scan every applicable `<GLOBAL_ID>_metadata.json`.** For each top-level content-type directory in scope:
   - Glob `<dir>/<PREFIX>-*/*_metadata.json`.
   - Parse JSON. If parse fails, skip (note as a warning at the end).
   - For each search field for that content type, test the query against the field's value.
   - Match logic:
     - Default: case-insensitive substring (`query.lower() in field.lower()`).
     - If `exact-match` is true: case-insensitive equality.

3. **Rank results.** Order by:
   1. Primary-field exact matches first.
   2. Primary-field substring matches second.
   3. Secondary-field matches third.

4. **Report** as a list:
   ```
   Found 3 match(es) for "Expire Servers":

   recurring_jobs/RJB-nsx4v2s1
     name: "Expire Servers"
     description: "Find servers that have expired and execute appropriate orchestration action"

   plugins/OHK-59t2apzf
     name: "Expire Servers Plugin"
     [referenced by recurring_jobs/RJB-nsx4v2s1 via dependencies.hook]

   ...
   ```

   When a returned plugin is referenced by another content unit, surface that relationship (run a quick reverse-grep for the plugin's path-form ID across all metadata files).

5. **No matches:** report clearly:
   ```
   No content found matching "Foo" in name/label/description fields.

   Suggestions:
     - Check spelling.
     - Try a shorter substring.
     - Use exact-match: false (default) — partial matches succeed.
     - List all content by type with: ls blueprints/ plugins/ ...
   ```

## Constraints

- Read-only — never modify files.
- Default to canonical top-level dir names. Skip absent dirs silently.
- Do not return Django model classes or internal IDs — only the on-disk folder paths.
- If multiple content types use the same search query, return all matches grouped clearly.
- For ambiguous cases (e.g. 12 matches), surface the top 10 and tell the user how to narrow.
