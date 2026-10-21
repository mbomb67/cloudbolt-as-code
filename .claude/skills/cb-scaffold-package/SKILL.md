---
name: cb-scaffold-package
description: Scaffold a CloudBolt content package (packages/PKG-*) for the Content Package Manager extension. Resolves the units the user names, keeps only top-level members (dropping whatever another member's export already carries), generates the PKG id, writes the manifest in the exporter's exact format plus a README, and explains how to import and sync it on an appliance.
when_to_use: When the user says "make a content package for X", "package the EKS content", "bundle these blueprints and actions so they sync together", "add a PKG manifest", or wants a named set of content to export, sync, or promote as one. Not for building an upload zip (cb-package-zip) or for adding content itself (the other cb-scaffold-* skills).
---

# cb-scaffold-package

Generate `packages/PKG-<id>/PKG-<id>_metadata.json` and `README.md` for a named set of content, ready for **Import from repository** in the Content Package Manager extension (`extensions/XUI-iq65xgvr`).

## Required reading

Load [docs/agents/metadata-schemas.md](../../../docs/agents/metadata-schemas.md) §15 (manifest format, fields, member rules), §16 (standalone parameter folders), §17 (Runbooks workflow folders) and §0 (ID generation, cross-reference shape). A manifest is not CloudBolt content: Source Control Repos ignores `packages/`, and only the extension reads it.

## Inputs

1. **Name.** Unique among packages (an appliance refuses a second package with the same name): `grep -h '"name"' packages/*/PKG-*_metadata.json`.
2. **Description.** One sentence saying what the package delivers; the catalog prints it.
3. **The content.** IDs, folders, names, or a subject ("everything for AWS EKS").
4. **Version.** Default `1.0.0`.

## Procedure

1. **Refresh the index.** `python tools/build_catalog.py`. The steps below read `catalog.json`, whose `uses` and `used_by` are each unit's dependency refs in both directions.

2. **Resolve the units** to `"<dir>/<GLOBAL_ID>"` refs with `cb-find-content-by-name`, or by searching `catalog.json` names and descriptions. For a subject, collect every unit whose name or description matches and show the list to the user before going on.

3. **Keep top-level members only.** Run the candidates through this check:

   ```bash
   python - blueprints/BP-r5apcf7e blueprints/BP-e4c0q58z extensions/XUI-8a1jocmr <<'EOF'
   import json, sys
   units = {u["path"]: u for u in json.load(open("catalog.json", encoding="utf-8"))}
   def closure(ref, key):
       seen, todo = set(), [ref]
       while todo:
           for nxt in units[todo.pop()][key]:
               if nxt not in seen:
                   seen.add(nxt)
                   todo.append(nxt)
       return seen
   picked = sys.argv[1:]
   for ref in picked:
       carriers = [p for p in picked if p != ref and ref in closure(p, "uses")]
       if ref.split("/")[0] in ("forms", "form_functions"):
           print(ref, "REPLACE with a parent:", ", ".join(sorted(closure(ref, "used_by"))))
       elif carriers:
           print(ref, "DROP, travels with", ", ".join(carriers))
       else:
           print(ref, "KEEP, carries", len(closure(ref, "uses")), "units")
   EOF
   ```

   - A form or form function never syncs on its own; replace it with the blueprint or action that uses it.
   - Drop anything another member carries: a blueprint brings its plugins, sub-blueprints, management actions, its own parameters, generated-options actions, form, form functions and shared modules; an action brings its plugin, form and shared modules; a parameter brings its generated-options actions; a Runbooks workflow brings the plugins its steps run; a blueprint also brings the workflows its Run Workflow items pin.
   - A plugin or shared module is a member only when nothing in the package references it. When the user names one, add the parent that belongs to the subject, not every parent in `used_by`: a shared dependency such as `orchestration_actions/HPA-qb0w86mi` (used by seven blueprints) is already covered by any member that carries it.

4. **Add what does not travel.** These must be listed or they stay behind:
   - Webhooks the members' forms call. A form calls `/cmp/inboundWebHooks/<uri_path>/run/`; find the paths in each member's form (`grep -o 'inboundWebHooks/[a-z0-9-]*' forms/FRM-<id>/FRM-<id>_metadata.json`) and add the `webhooks/IWH-*` whose `uri_path` matches.
   - UI extensions for the subject (an XUI references nothing and nothing references it).
   - Standalone orchestration actions, recurring jobs, flow control actions, server actions and MCP tool actions for the subject: `catalog.json` entries with an empty `used_by` that match it. Ask the user when one is borderline.
   - Parameters the content reads that no blueprint defines, such as an environment or group parameter an orchestration action keys on (`grep -rn "get_value_for_custom_field\|get_cf_value\|CustomField.objects" plugins/OHK-<id> shared_modules/SHM-<id>` over the members' plugins and shared modules, and the prerequisites in their READMEs). List each as `parameters/CF-<id>` when `parameters/CF-<id>/` is in the repo. When it is not, the folder has to come from an appliance: tell the user to add the parameter to the package on the Content Packages page and Export, which writes it; do not write that folder by hand (§16).
   - Runbooks workflows the content runs that no blueprint member pins: one the user names, or one users run with the Run Workflow server action. A workflow a blueprint member's Run Workflow build item (`plugins/OHK-69gk5ne9`) pins, as a default on its `workflow` input or a `plugin-bdi-<item>.workflow` question in the blueprint's form, travels with the blueprint; do not list it. List each other one as `runbook_workflows/SWF-<id>` when that folder is in the repo. When it is not, tell the user to add the workflow to the package on the Content Packages page and Export, which writes it (§17). The package then also needs the Runbooks content (`packages/PKG-v20qv308`) on the target appliance.

5. **Generate the ID** per [AGENTS.md cardinal rule 6](../../../AGENTS.md#cardinal-rules), never hand-typed:

   ```bash
   python -c "import random,string,sys; a=string.ascii_lowercase+string.digits; [print(p+'-'+''.join(random.choices(a,k=8))) for p in sys.argv[1:]]" PKG
   ```

   Grep the repo for the suffix and regenerate on a collision.

6. **Write the two files** with this script, so the manifest is byte-for-byte what the exporter writes (`json.dumps(indent=4, sort_keys=True)` plus a newline) and the README is the one it writes on a first export (`services._readme`). It refuses to overwrite an existing folder. Arguments: id, name, description, version, then the members in order (sync runs shared modules, then plugins, then the rest in this order, parameters and workflows after the actions, blueprints last).

   ```bash
   python - PKG-<id> "<name>" "<description>" 1.0.0 blueprints/BP-... extensions/XUI-... <<'EOF'
   import datetime, json, os, sys
   pkg_id, name, description, version, *refs = sys.argv[1:]
   units = {u["path"]: u for u in json.load(open("catalog.json", encoding="utf-8"))}
   kinds = {"blueprints": "Blueprint", "resource_actions": "Resource action", "server_actions": "Server action",
            "orchestration_actions": "Orchestration action", "flowcontrol_actions": "Flow control action",
            "recurring_jobs": "Recurring job", "cit_tests": "CIT test", "webhooks": "Inbound webhook",
            "mcp_tool_actions": "MCP tool action", "shared_modules": "Shared module", "plugins": "Plugin",
            "extensions": "UI extension", "parameters": "Parameter", "runbook_workflows": "Runbook workflow"}
   manifest = {
       "id": pkg_id, "name": name.strip(), "description": " ".join(description.split()), "version": version,
       "minimum_version_required": "", "last_updated": datetime.date.today().isoformat(),
       "metadata_version": "2026.1.0", "contents": refs,
   }
   folder = os.path.join("packages", pkg_id)
   os.makedirs(folder)
   with open(os.path.join(folder, pkg_id + "_metadata.json"), "w", encoding="utf-8", newline="\n") as fh:
       fh.write(json.dumps(manifest, indent=4, sort_keys=True) + "\n")
   lines = ["# " + manifest["name"], ""]
   if manifest["description"]:
       lines += [manifest["description"], ""]
   lines += ["Content package `%s`, version %s. Sync it from the Content Packages admin page "
             "(Content Package Manager UI extension)." % (pkg_id, version), "",
             "| Type | Name | Folder |", "|---|---|---|"]
   for ref in refs:
       name_cell = units[ref]["name"].replace("|", "\\|")
       lines.append("| %s | %s | [%s](../../%s/) |" % (kinds[ref.split("/")[0]], name_cell, ref, ref))
   with open(os.path.join(folder, "README.md"), "w", encoding="utf-8", newline="\n") as fh:
       fh.write("\n".join(lines) + "\n")
   print("wrote", folder)
   EOF
   ```

7. **Validate and index.**

   ```bash
   python tools/validate_metadata.py
   python tools/build_catalog.py
   ```

   Every `PROBLEM` must be fixed. A `WARNING ... already travels with` means step 3 kept a carried unit: delete the folder and rerun step 6 without it.

8. **Report** the folder, the members and what each carries, what you dropped or added and why, and the steps below.

## Getting the package onto an appliance

The extension must be installed first: sync `extensions/XUI-iq65xgvr` like any UI extension and restart the web server (`systemctl restart httpd`), because it adds URL paths. Push the package on a branch whose name has no slash, or merge it to `main`.

- **UI:** Admin > Custom Extensions > Content Packages > **Import from repository**. Pick the repository and branch, then the package. It registers under its PKG id (updating the package if that id is already registered) and opens **Sync**, which starts one sync job per member, chained.
- **JSON API** (CB Admin, session cookie plus the `X-CSRFToken` header; `<repo>` is the Source Code Repository's numeric id or `SCR-` global id):

  | Call | Body | Returns |
  |---|---|---|
  | `GET /xui/content_packages/api/repos/<repo>/packages/?branch=<branch>` | | The manifests under `packages/` on that branch |
  | `POST /xui/content_packages/api/repos/<repo>/packages/import/` | `{"branch": "<branch>", "ref": "packages/PKG-<id>"}` | The registered package, `created` true or false |
  | `POST /xui/content_packages/api/packages/PKG-<id>/sync/` | `{"refresh_if_exists": true, "ignore_action_enabled": false}`; repository and branch default to where it was imported from | `{"jobs": [{"ref", "via", "job_id", "url"}, ...]}`, in run order; `via` names the parameter a generated-options action syncs for |

Do not create the package with **Add** or `POST /xui/content_packages/api/packages/`: the appliance mints its own PKG id, so the repo manifest and the appliance definition would be two packages.

## Constraints

- IDs from the generator in step 5, never hand-typed.
- Never `forms/` or `form_functions/` members, never duplicates, never a member another member carries.
- Every member must exist in the repo (the validator checks it). The appliance does not need it yet; Sync creates it.
- A `parameters/CF-*` or `runbook_workflows/SWF-*` folder comes from an appliance export, never from this skill.
- Write the files with the step 6 script, not by hand, so the format matches the exporter. An appliance export later replaces the manifest with its own definition and rebuilds only the README's member table (from the `| Type | Name | Folder |` header to its last row), so keep that header and keep hand-written text outside the table.
