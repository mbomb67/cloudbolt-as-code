# Source query: CloudBolt zip import format

Paste the prompt below into a Claude session opened on the CloudBolt CMP source checkout (the Django `src/` tree). It re-derives the zip package format from the importer code and returns a report in the same shape as [zip-package-format.md](zip-package-format.md), so the two can be diffed and `tools/package_zip.py` updated. Run it after a CloudBolt upgrade or whenever a zip built by the tool is rejected.

---

You are reading the CloudBolt CMP source (Django). Document, with `path:line` citations, exactly what the **v3 zip import** accepts for every content type that supports it, so a tool that converts Source Control Repos content back into uploadable zips can be verified against this version. Read-only; cite code, do not paraphrase from memory. Start by printing the version (`git describe --tags`).

**Where to look**

- Generic importer: `api/v3/mixins.py` (`ImportExportMixin.import_resource`, `import_single_object`, `extract_archive_metadata_and_files`, `shared_module_dependencies`), `api/v3/viewsets.py` (`ImportExportViewsetMixin._create`: multipart field names and query flags), `utilities/cb_http.py` (`zip_package`, `zip_files`, `response_attachment_to_file`: how member names are built).
- Actions: `cbhooks/api/v3/serializers/action_base.py` (`HasBaseActionSerializer.export`, `import_base_action`, custom-form pull), `orchestration_hook.py` (`BaseActionSerializer.export`, `create_resource_from_metadata`, `add_scripts_if_needed`, `import_hook_inputs`), and the per-type files `hook_point_action.py`, `resource_action.py`, `server_action.py`, `recurring_action_job.py` + `jobs/api/v3/serializers/recurring_job.py`, `inbound_web_hook.py`, `mcp_tool_action.py`, `flowcontrol_actions.py`, `shared_module.py`, `cscv/api/v3/serializers/action_cit.py`.
- Blueprints: `servicecatalog/api/v3/serializers/service_blueprint_import_export_mixin.py` (`import_resource`, `export`, every `export_*_as_temp_files`, `create_resource_from_metadata`, `_import_blueprint_dependencies`, `export_to_filesystem_as_unzipped_files_flattened`) and `service_item_mixin.py` (`from_dict`: how each deployment item finds its nested zip).
- UI extensions and forms: `extensions/api/v3/serializers.py` (`UIExtensionSerializer.export`, `extract_files_from_zip`, `create_resource_from_metadata`, `refresh_action_from_local_path`), `customforms/api/v3/serializers/custom_forms.py` and `form_functions.py`.
- Source Control Repos (the repo layout): `source_code_repos/models.py` (`get_source_repo_operations_details`, `path_to_*` defaults) and each serializer's `export_to_filesystem_as_unzipped_files_flattened` / `refresh_action_from_local_path`.
- Upload entry points: `cbhooks/views.py` (`upload_action_trigger` branches), `servicecatalog/views.py` (`upload_blueprint`), `extensions/views.py`, and the `urls.py` under each `api/v3/`.

**Answer these, per content type (BP, OHK, SHM, HPA, RSA, SVA, RJB, IWH, MTA, FCA, CIT, XUI, FRM, FJS)**

1. Package naming: how the zip's folder and JSON names are derived (slugify rules; which field, `name` or `label`).
2. The exact member list the exporter writes, including every nested-zip name pattern and its prefix (`build_<seq>_`, `teardown_<seq>_`, `management_`, `discovery_`, `genoptions_`, `environment_selection_<seq>_`, `ratehook_<seq>_`, `custom_form_`, `condition_`, `shared_module_`, `form_function_`, `<name>.zip` for XUI, or whatever this version uses).
3. How the importer locates each nested zip: by member-name prefix, by exact name, by position in the archive, or by a JSON key. Quote the lookup line.
4. Every JSON key the importer **reads** (not just what export writes), marking required keys, defaults, and value shapes; the `id` prefix check; which keys are informational.
5. Keys that must be **absent** or are harmful (for example a `metadata_version` that switches the blueprint importer into the flattened mode and skips nested zips).
6. Mapping from the flattened Source Control Repos file (`<ID>_metadata.json` with `dependencies` paths like `plugins/OHK-xxxx`) to the zip: for each `dependencies` key, the nested zip it becomes and any JSON key the item must carry (`rate_action_name`, `environment_selection_orchestration.title`, `functions[]`, `shared_module_dependencies[]`, …).
7. Validation and replace semantics: `replace_existing` / `protect_existing` / `ignore_action_enabled`, name-collision behaviour, anything that silently drops data (defaults matched by `_a<n>` prefix, `formatter_pattern` vs `value_pattern_string`, placeholder strings skipped, encrypted `PWD`/`ETXT` defaults, `enabled` overridden by a preference).
8. Which UI page and which v3 API route accept the zip for that type, and which types have no zip import at all.

**Output**: one markdown report with a section per type, each with a member-list block, a key table (key, required, read-at `path:line`, notes), and a gotchas list. End with a "changes since the previous reference" section if you were given the previous `zip-package-format.md` to compare against.
