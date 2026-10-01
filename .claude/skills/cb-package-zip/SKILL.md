---
name: cb-package-zip
description: Build a zip that CloudBolt's Import button (or POST /api/v3/cmp/<type>/) accepts from a content unit in this repo — a blueprint, action, plugin, shared module, or UI extension — by converting the Source Control Repos layout into CloudBolt's nested package format with every transitive dependency bundled, then reporting what must be re-entered after import.
when_to_use: When the user says "create a zip", "package this blueprint", "export this for upload", "I need to upload this to CloudBolt", "zip up this action", or wants to move content to a CloudBolt instance that is not synced to this repo. Not for syncing a repo (that is CloudBolt's Source Control Repos feature) and not for forms or form functions on their own (they only import inside a parent).
---

# cb-package-zip
Turn `<dir>/<ID>/` plus its `dependencies` closure into one uploadable zip. The repo stores a unit as a flat folder with `"<dir>/<ID>"` references; CloudBolt's importer wants one `<slug>/<slug>.json` package per unit with dependencies nested as prefixed inner zips and no `dependencies` or `metadata_version` keys. `tools/package_zip.py` does the conversion and verifies the result the way the importer reads it.

## Required reading

Load [docs/agents/zip-package-format.md](../../../docs/agents/zip-package-format.md): §1 for the package shape and how the importer reads a layer, §2–§3 for the per-type and blueprint mapping tables, §4 for what does not survive the trip, §5 for where each type is uploaded. The tool encodes those rules; read the doc when its output needs explaining or a zip is rejected.

## When to use

- Moving one blueprint or action to a CloudBolt instance (demo, customer, lab) without connecting it to this repo.
- Handing content to someone who will upload it through the UI.
- Checking what a unit drags along: `--dry-run` prints the full nested tree and the bundled dependency list.

## Inputs

1. **Target** (required): a repo path (`blueprints/BP-5pei9cno`), a global ID (`BP-5pei9cno`), or a name/label (`"Azure Cross-Tenant Subscription"`). Ambiguous names are listed back; pick one. Use `cb-find-content-by-name` if the user only has a rough description.
2. **Output directory** (default `dist/`, gitignored).
3. **Replace or add** on the target instance: this only affects how the user uploads (`replaceExisting=true` matches by global ID, then name, and updates in place; otherwise CloudBolt creates a copy named "… (2)"). Ask when the content already exists there. **Never replace a blueprint the target already syncs from a Source Control Repo**: the importer then follows its repo-refresh path and fails with `Failed to fetch remote file for action … file:///var/opt/cloudbolt/repos/…`. Import without replace, or remove the synced copy first.

## Procedure

1. **Preview the package.**

   ```bash
   python tools/package_zip.py BP-5pei9cno --dry-run
   ```

   Read the tree: every `build_<seq>_…`, `teardown_<seq>_…`, `management_…`, `discovery_…`, `genoptions_…`, `custom_form_…`, `shared_module_…` member is one bundled dependency. Confirm with the user that nothing unexpected is included and nothing expected is missing (a management action without `dependencies.resource_action`, a plugin without `dependencies.sharedModules` for a module it imports).

2. **Fix the repo, not the zip, when the tool stops.** Exit 1 means the content cannot be packaged as-is; the message names the unit and key:
   - a dangling or wrong-type reference → correct `dependencies` in the metadata (then `python tools/validate_metadata.py`);
   - two items sharing a `deploy_seq` or a sub-blueprint seq that prefixes another (`build_1` vs `build_10_…`) → renumber `deploy_seq`;
   - a `tfconfig`/`tfoperation`/server-provider item → that blueprint must be exported from CloudBolt itself;
   - `forms/` or `form_functions/` as the target → package the parent the tool names;
   - a CIT test → there is no zip import; sync `cit_tests/` from the repo.

3. **Build it.**

   ```bash
   python tools/package_zip.py BP-5pei9cno
   ```

   The tool re-opens the finished zip and checks the importer's invariants (one JSON per layer, unique member basenames, `script_filename` present, no `metadata_version`, valid ID prefixes). Several targets can be passed at once; each becomes its own zip.

4. **Report to the user**, in this order:
   - the zip path and the "Upload via" line (UI page and API route for that type; plugins and flow-control actions are API-only, shared modules UI-only);
   - the **After import** list verbatim: redaction placeholders to re-enter, `PWD`/`ETXT` defaults that will not decrypt, hook points / OS builds / MCP tool names that must exist on the target, the web-server restart for UI extensions;
   - any **Warnings** (dropped `gen_options_hooks` with no action, files listed in `package_contents` but missing on disk, `enabled: false` ignored for XUI).

5. **API upload, when asked.** Multipart `zipFile`, optional `replaceExisting=true`, `ignoreActionEnabled=true`:

   ```bash
   curl -sS -H "Authorization: Bearer $CB_TOKEN" -F "zipFile=@dist/azure_cross_tenant_subscription.zip" -F "replaceExisting=true" "https://<cloudbolt>/api/v3/cmp/blueprints/"
   ```

   Do not run it against a customer instance without the user's go-ahead; it creates or overwrites content.

## Constraints

- Never hand-edit the produced zip or the generated JSON; change the repo metadata and rebuild, so the repo stays the source of truth.
- Do not add README files, extra JSON, or directory entries to a package: every `.json` member is read as metadata and the first non-JSON member of an action package must be the plugin zip.
- The tool runs from the repo root and writes only under `--out` (default `dist/`, gitignored). Do not commit zips.
- The format reference is derived from CloudBolt v2026.3; if a zip is rejected by a newer appliance, re-run [docs/agents/zip-package-format-source-query.md](../../../docs/agents/zip-package-format-source-query.md) against the CloudBolt source and update the doc and tool together.
