# Content Package Manager — design brainstorm

Status: brainstorm, 2026-10-15. Steps 1 to 3 of the first cut are built in
`extensions/XUI-iq65xgvr`. Where the build differs from this draft (packages
stored in a namespaced CustomField, one sync job per member, export run as a
job, card-style pages), the extension's README and code are current.
Standalone parameters were added on 2026-10-16 (see below).
Step 4 is built: the AGENTS.md rule, `docs/agents/metadata-schemas.md` §15,
the catalog section (member back-references are a `packages` key in
`catalog.json`, not a column), the validator's package checks, the
`cb-scaffold-package` skill, and `packages/PKG-bo8ensty` for AWS EKS.

## Problem

A solution such as AWS EKS is one blueprint plus a namespace blueprint, a UI
extension, eight resource actions, an orchestration action, plugins and shared
modules. CloudBolt's Source Control Repos feature pushes and refreshes **one
content unit at a time** (a blueprint drags its own plugins and forms along, but
nothing links the blueprint to the XUI or to a standalone orchestration action).
Moving "the EKS solution" between appliances means remembering the list.

A **package** is that list, stored in the repo next to the content it names, so
it can be pushed, refreshed and promoted as one thing.

## What already exists (reuse, do not rewrite)

Everything below is in `source_code_repos/` on the appliance.

| Need | Existing plumbing | Where |
|---|---|---|
| Push one unit to a repo branch, with its dependency closure | `obj.push_to_remote_source(repo, branch, commit_msg, profile, repo_dir)` on every syncable model (`SourceCodeRepoMixin`) and on `ServiceBlueprint`. Exports via the type's serializer, commits, pushes, then stamps `remote_source_url` + `source_code_repo` on the unit and each exported dependency. | `source_code_repos/models.py:1528`, `servicecatalog/models.py:2707` |
| Which serializer and repo folder a type uses | `get_source_repo_operations_details()` → `RepoOperationsInfo(serializer_cls, repo_path_field, type_title)` | `source_code_repos/models.py:1384` |
| Refresh many units from a branch in one clone | `SourceCodeRepoService(repo, profile).sync_from_repo(branch, objects_to_sync, refresh_if_exists, ignore_action_enabled)`; `objects_to_sync` is `{type_key: ["<ID>/<ID>_metadata.json", ...]}` with keys from `OBJECT_TYPE_MAPPINGS` plus `"blueprints"` | `source_code_repos/services.py:112` |
| Run that sync as a job with a log | `SourceCodeRepoSyncParameters` + `Job(type="sourcecodereposync")`, exactly what `POST /api/v3/cmp/sourceCodeRepos/<id>/syncFromRepo/` does | `source_code_repos/api/v3/viewsets/source_code_repo.py:38`, `jobs/models.py:688` |
| One shared clone for several pushes | `prepare_repo_from_url_and_branch(branch, profile, repo_dir)` reuses the directory when it already exists | `source_code_repos/models.py:861` |
| Read files from any branch without a full clone | `repo.get_global_clone()` + `git ls-tree --name-only -r origin/<branch> -- <dir>` + `git show origin/<branch>:<path>`, the technique behind the OOB metadata-file picker | `source_code_repos/views.py:468` |
| Branch list, PR link | `repo.get_branches()` (fetches first), `repo.get_pull_request_url(branch)`, URL name `json_branches_by_repo_id` | `source_code_repos/models.py:565` |
| Admin page, dialogs, JSON views | `@admin_extension` (Admin > Custom Extensions), `@dialog_view` + `open-dialog` anchors, `@json_view`, `@cbadmin_required`, `xui_urlpatterns` | `extensions/views.py:164`, `utilities/decorators.py`, `utilities/permissions.py:759` |

Dependency closure is already handled by the serializers: a blueprint export
writes its deployment/teardown/discovery plugins, management actions, generate
options plugins, forms, form functions and shared modules to their own folders
and returns them (`export_to_filesystem_as_unzipped_files_flattened`). An action
export does the same for its plugin and shared modules. A UI extension has no
dependencies. So a package lists **top-level units only**; the closure travels
with them on export and is pulled back by the same serializers on sync.

## Repo format

One folder per package under a new top-level dir, following the repo's
ID-prefixed convention:

```
packages/
└── PKG-x7k2m9qa/
    ├── PKG-x7k2m9qa_metadata.json
    └── README.md
```

```json
{
    "id": "PKG-x7k2m9qa",
    "name": "AWS EKS",
    "description": "EKS cluster and namespace blueprints with the cluster UI extension and day-2 actions.",
    "version": "1.0.0",
    "minimum_version_required": "8.6",
    "last_updated": "2026-10-15",
    "metadata_version": "2026.1.0",
    "contents": [
        "blueprints/BP-r5apcf7e",
        "blueprints/BP-e4c0q58z",
        "extensions/XUI-8a1jocmr",
        "orchestration_actions/HPA-qb0w86mi",
        "resource_actions/RSA-p2fnjwj3"
    ]
}
```

Rules:

- `contents` entries are the repo's cross-reference strings
  (`"<top_level_dir>/<GLOBAL_ID>"`), nothing else. The catalog builder's
  `REF_RE` and the metadata validator already understand this shape.
- `forms/` and `form_functions/` are not allowed as members. They only sync as
  dependencies of a parent, so listing them would be a no-op on import.
- `plugins/` and `shared_modules/` are allowed but rarely needed; include a
  plugin only when nothing else in the package pulls it in.
- The ID is `PKG-` plus 8 characters of entropy from the same generator as
  every other global ID (cardinal rule 6). `PKG` is not a CloudBolt prefix;
  nothing on the appliance will collide with it.
- `README.md` is generated on first export from the manifest (name,
  description, one table row per member with its name and type). Later exports
  rebuild only its member table (the block from the `| Type | Name | Folder |`
  header through its last row, `services.refresh_readme`), so members added
  since, such as a parameter, are listed, and hand edits around the table
  survive.
- The directory name is fixed at `packages/`. `SourceCodeRepo` has a
  `path_to_*` field per OOB type but no field for this, and an XUI cannot add
  one. An XUI setting can override it per repo if a customer ever needs that.

## Where package definitions live on the appliance

An XUI cannot add a Django model (no migrations), so the definitions need a
store that survives an XUI re-sync and a CloudBolt upgrade.

**Decision: one JSON file per package under `settings.PROSERV_DIR` +
`content_packages/`**, written atomically (temp file + rename). The file is the
manifest above plus a `local` block the export strips:

```json
"local": {
    "source_code_repo_id": 3,
    "branch": "main",
    "remote_source_url": "https://github.com/.../blob/main/packages/PKG-x7k2m9qa/PKG-x7k2m9qa_metadata.json",
    "last_exported": "2026-10-15T14:02:11Z",
    "last_synced": "2026-10-15T14:40:00Z",
    "last_sync_job_id": 48213
}
```

That mirrors how every synced unit already carries `source_code_repo` +
`remote_source_url` + `last_cached`, so a package behaves like any other
remote-sourced object: it has a home branch, it can be pushed, it can be
refreshed.

Considered and rejected:

- *Repo only, no local store.* Every edit becomes a commit and the list page
  needs a repo and branch before it can show anything. A package must be
  draftable before it is published.
- *Unmanaged model + raw `CREATE TABLE` at import.* Gives an ORM, but no
  migration owns the table, so upgrades and support tooling do not know it
  exists. Revisit only if this moves into the product.
- *`UIExtension.extra_metadata`.* Overwritten by the XUI's own re-sync.

## The XUI

`extensions/XUI-<id>/content_packages/` with `views.py`, `urls.py`, `forms.py`,
`store.py` (the JSON store), `services.py` (export/sync), `templates/`.
It ships in this repo like every other XUI and becomes the first member of a
package named "Content Package Manager".

### Admin page

`@admin_extension(title="Content Packages")`, cbadmin only. Rendered at
`/extensions/admin/content_packages/<view>`, listed under Admin > Custom
Extensions. Bootstrap 5, theme tokens, `v5-datatable` like the OOB Source
Code Repositories list.

```
Content Packages                                  [ Import from repository ]  [ + Add ]

Name          Description                 Contents                         Repository           Actions
AWS EKS       EKS cluster and namespace…  BP AWS EKS Cluster               github-cb-as-code    [Edit] [Export] [Sync] [Delete]
                                          BP AWS EKS Namespace             main
                                          XUI AWS EKS Cluster              exported 2h ago
                                          RSA Upgrade AWS EKS Nodegroup    synced 1h ago
                                          HPA … +1 more
ServiceNow…   REQ/RITM tracking …         SHM … RJB … HPA … +4 more        —  (never exported)  [Edit] [Export] [Sync] [Delete]
```

The Contents cell is a stack of pills: type badge (BP, RSA, XUI, HPA, …) plus
the unit's current name, capped at five with "+N more" that expands. A pill
goes red with a tooltip when the unit no longer exists on this appliance
(deleted after the package was defined), and amber when the unit's
`remote_source_url` is on a different repo or branch than the package's
(unpushed drift). Those two states are the reason to resolve names at render
time rather than store them.

### Dialogs (all `@dialog_view`, opened with `open-dialog` anchors)

- **Add / Edit package.** Name, description, version, and the content picker:
  one collapsible section per type (Blueprints, Resource actions, Server
  actions, Orchestration actions, Flow control, Recurring jobs, CIT tests,
  Webhooks, MCP tool actions, Shared modules, Plugins, UI extensions), each a
  filterable checklist with a count badge. Rows show the unit's name and,
  when it is already synced, the repo label as a muted suffix. Querysets
  mirror the OOB repo Contents tab: `ServiceBlueprint(status="ACTIVE")`,
  `ResourceAction`, `ServerAction`, `HookPointAction`, `FlowControlAction`,
  `RecurringActionJob`, `ActionCITTest`, `InboundWebHook`, `MCPToolAction`,
  `SharedModule`, `UIExtension`, standalone plugins
  `OrchestrationHook(shared=True)`. Wide dialog (`data-dialog-width`).
- **Export.** Same shape as the OOB "Push to repository" dialog
  (`cbhooks/forms.py:3440`): repository, existing branch or new branch name
  (branches via `json_branches_by_repo_id`), commit message. Runs the export
  below, shows per-member status and the "Open a Pull Request" link from
  `get_pull_request_url`.
- **Sync.** Repository and branch default to the package's home; checkboxes
  for `refresh_if_exists` (default on) and `ignore_action_enabled` (default
  off), the two OOB sync flags. Creates the sync job and links to it.
- **Import from repository.** Repository, branch, then a list of manifests
  found under `packages/` on that branch (name, description, member count,
  already-registered marker). Picking one registers it locally with its home
  repo and branch and opens the Sync dialog prefilled.
- **Delete.** Removes the local definition only. The repo folder and the
  content on the appliance are untouched; the dialog says so.

### API (`urls.py` → `xui_urlpatterns`)

All under `xui/content_packages/api/`, `@cbadmin_required`, JSON in and out.
Session auth with CSRF like every other XUI endpoint. Mounting them as DRF
`APIView`s with CloudBolt's API token auth and `CloudBoltAPIAdminPermission`
would let scripts call them with a bearer token; worth doing once the shapes
settle (verify the auth class CloudBolt's v3 viewsets use before relying on it).

| Method, path | Does |
|---|---|
| `GET /packages/` | List, with resolved member names, drift and missing flags, home repo and branch, last export and sync. |
| `POST /packages/` | Create: `{name, description, version, contents[]}`. Validates every ref resolves on this appliance and is an allowed type. |
| `GET /packages/<pkg>/` | One package. |
| `PATCH /packages/<pkg>/` | Update any of the four fields. |
| `DELETE /packages/<pkg>/` | Drop the local definition. |
| `GET /content/` | Everything the picker can choose, grouped by type: `{ref, name, type_title, repo_label}`. |
| `GET /repos/<repo_id>/packages/?branch=` | Manifests under `packages/` on that branch of the repo, read from the global clone after a fetch. |
| `POST /repos/<repo_id>/packages/import/` | `{branch, ref}` registers that manifest locally and returns it. |
| `POST /packages/<pkg>/export/` | `{source_code_repo_id, branch, new_branch, commit_message}` → per-member result plus PR URL. |
| `POST /packages/<pkg>/sync/` | `{source_code_repo_id?, branch?, refresh_if_exists?, ignore_action_enabled?}` → `{job_id, job_url}`. |

Branch listing stays on the OOB endpoint (`json_branches_by_repo_id`).

## Flows

### Export (push the package and everything in it)

1. `TemporaryDirectory(dir=settings.REPO_DIR)` as the shared `repo_dir`.
2. For each member, resolve the model by global ID and call
   `member.push_to_remote_source(repo, branch, commit_message, profile, repo_dir=repo_dir)`.
   The first call clones and checks out (creating the branch if new); later
   calls reuse the clone. Each call commits and pushes that unit and its
   dependency closure and stamps the remote URLs. Collect `(status, msg)` per
   member; keep going on failure so one bad unit does not hide the rest.
3. Write `packages/PKG-<id>/PKG-<id>_metadata.json` (manifest without
   `local`) and `README.md` (whole if absent, else its member table rebuilt)
   into the same clone, then
   `repo.commit_and_push(branch, commit_message, profile, repo_dir=repo_dir)`.
4. Update the package's `local` block: repo, branch, `remote_source_url` built
   with `get_repo_url_for_file(branch, <manifest path>)`, `last_exported`.

This yields one commit per member plus one for the manifest. That is the cost
of using `push_to_remote_source` unchanged. A single-commit variant is about
twenty lines (call each type's
`export_to_filesystem_as_unzipped_files_flattened` into the shared clone with
the kwargs `push_to_remote_source` builds, one `commit_and_push`, then stamp
`remote_source_url` on each unit and returned dependency). Start with the
per-member version; switch when the commit noise matters.

Timing: each member push is a network round trip, so a ten-member package can
take a minute or more inside a web request. Two ways to keep it off the
request thread, in order of preference:

- Ship a plugin ("Export Content Package") with the XUI and start it with
  `run_hook_as_job` (the mechanism the MCP tool actions and resource actions
  use); the export then has a job log and history like Sync does.
- Synchronous view with a progress spinner, acceptable for the first cut
  while the package count is small.

Known behavior applies here: an appliance export rewrites every
member folder, including hand-authored READMEs and form fields. Export always
goes to a branch and the PR diff is the review step.

#### Keeping READMEs through an export

Why they disappear (CloudBolt v2026.3.83): every flattened export deletes the
unit's folder before writing it (`shutil.rmtree` in
`service_blueprint_import_export_mixin.py:2542`, `action_base.py:355`,
`orchestration_hook.py:934` for plugins and shared modules,
`custom_forms.py:325` for forms), and `SourceCodeRepo.commit` stages with
`git add -A` (`source_code_repos/models.py:812`), so the deletion is
committed. Export, commit and push all happen inside one
`push_to_remote_source` call, so nothing can run between them. UI extensions
are the exception: only `<XUI-id>/<package>/` is replaced, so the README beside
the metadata survives. Filed as CMPITK-1424.

Built (`services.restore_docs`): keep the OOB push and undo its README
deletions in the manifest commit the export already makes.

1. Before the first push, fetch the global clone and record the base: the
   SHA of `origin/<branch>`, or of the default branch (`origin/HEAD`) when the
   push will create the branch. If neither can be read, nothing is restored
   and the job says so.
2. Push the members unchanged.
3. In the manifest clone, list files deleted between the base and `HEAD` by
   this export's own commits (`git log --diff-filter=D --name-only base..HEAD
   --fixed-strings --grep "Content package <name> (<PKG-id>):"`), keep the
   `*.md` files still missing at `HEAD` and the files those READMEs link to
   relatively (diagrams), and restore them with `git checkout <base> -- <paths>`.
4. Commit them with the manifest and report the restored paths in the job
   output and on the details page's Last export card.

The PR's net diff then shows no README deletions; the member commits still
show the delete, and a squash merge hides it. Restoring only deletions made
by this export's commits leaves a README someone else deleted on the branch
alone.

Rejected: re-implementing the push to make one clean commit (duplicates
`push_to_remote_source`'s remote-URL, history and alert bookkeeping);
patching `SourceCodeRepo.commit` during the export (the job engine runs jobs
as threads in one process, so the patch would leak into other pushes); moving
READMEs out of the content folders (the Git host stops showing them with the
folder). An export from CloudBolt's own Push to repository buttons bypasses
the extension, so a repo-side CI check that names deleted READMEs and the
`git checkout` that restores them is the complement.

### Standalone parameters

Added 2026-10-16. Parameter-driven orchestration (for example the `disk_map`
environment parameter behind `packages/PKG-zny1ynfs`) needs the parameter on
the target appliance, often with the orchestration action that generates its
options, and nothing in a blueprint carries it.

What CloudBolt has (v2026.3): `SourceCodeRepo` has no `path_to_*` for
parameters and `model_repo_mapping` / `OBJECT_TYPE_MAPPINGS` do not list
them, so Source Control Repos cannot push or sync one on its own. A
parameter reaches a repository only inside a blueprint's `parameters[]` or an
action's `action_inputs[]`. The serializer behind `parameters[]` is reusable
as is: `infrastructure.api.v3.serializers.CustomFieldSerializer`, with
`resource_dict(cf, exporting=True, include_dependencies=True)` on export
(definition, OS families, field dependencies, global options, and each
generated-options action under `gen_options_hooks[].dependencies.orchestration_hook`)
and `get_or_create_from_dict(data, replace_existing)` on import (matches by
name, keeps the exported global ID only when it creates). Blueprint import
links an action with `hpa.hook.custom_fields.add(cf)` after removing the
parameter's current generated-options plugins.

Built:

- A member type `parameters/` with prefix `CF`; the picker offers Admin >
  Parameters (`CustomField.custom_field_list_parameters`), refs resolve to any
  parameter outside a namespace (action inputs live in `action_inputs`).
- Export writes `parameters/CF-<id>/CF-<id>_metadata.json` in the manifest
  commit: the serializer's export, plus `global_defaults` and
  `global_constraints` as the Parameters API returns them, with each action
  rewritten to `orchestration_actions/HPA-<id>` the way the blueprint export's
  `_process_dependencies` does. Each generated-options action is pushed with
  the OOB per-object push first, unless the package lists it. A README is
  written when the folder has none.
- Sync reads each parameter's file from the global clone before creating any
  job, adds an OOB sync job for each generated-options action the package
  does not list (after the other actions), then one job per parameter (before
  blueprints) of a second self-registering plugin, "Content Package Manager:
  Sync Parameter" (`parameter_job.py`). It is queued like `run_as_job`
  queues a plugin, plus `prereq_job`, so it sits in the same chain and Rerun
  works the same. The job runs `get_or_create_from_dict` (update only with
  "refresh content that already exists"; a type change is refused, as the
  Parameters API refuses it), then the serializer's own
  `update_global_options` / `update_global_defaults` /
  `update_global_constraints`, then links the actions.
- A parameter matched by name to one with another global ID keeps its ID
  (custom forms on that appliance reference it); every package listing the
  ref records `local.aliases[<package ID>] = <local ID>` and resolves
  through it.

Left out, as CloudBolt leaves them out of every export: environment, group
and blueprint assignments. Also left out: option and default values of
password and encrypted-text parameters (secrets) and of types whose values
are appliance objects (network, LDAP, NSX, storage, file, AAP).

Rejected: a parameter's own OOB push (none exists); embedding parameters in
the package manifest (a parameter can belong to several packages, and the
repo convention is one ID-prefixed folder per unit); syncing generated-options
actions inside the parameter job with `refresh_action_from_local_path`, the
blueprint import's internal path (per-object OOB sync jobs keep a job log and
Rerun per action); changing a matched parameter's global ID.

### Runbooks workflows

Added 2026-10-21 (phase 1 of packaging the Runbooks content). A Runbooks
workflow (`SWF-*`) is a JSON value of the `runbook_workflow_store`
parameter, kept by the `runbook_workflows` shared module
(`shared_modules/SHM-nznzhpp0`). Source Control Repos never sees it, and its
steps run plugins (Remote Scripts, plug-ins, webhooks, emails, copy-file
actions) that no other member exports.

Built, on the parameters pattern:

- A member type `runbook_workflows/` with prefix `SWF`, not backed by a model:
  `MemberType.lookup` replaces the global ID query, and `store.Workflow` wraps
  each stored workflow. Without the Runbooks content the type offers nothing.
- `runbook_workflows` owns the format. `export_workflow` writes step actions
  by global ID and name, OS family / technology / OS build filters by name
  (with the families and technologies only OS builds or environments
  implied), and only which group, environment and configuration manager
  filters were set. `import_workflow` creates or updates.
- `create_workflow(..., workflow_id=None)`: a package sync passes the source
  ID, so a blueprint pinned by ID finds the workflow on every appliance.
  Add, Copy and the REST API never pass one. An edit (`update_workflow`)
  keeps the ID as before.
- Export pushes each step's plugin with the OOB per-object push (unless the
  package lists it), then writes `runbook_workflows/SWF-<id>/` in the
  manifest commit with the plugins under `dependencies.hooks`.
- Sync adds an OOB sync job per step plugin with the other actions, then one
  job of a third self-registering plugin, "Content Package Manager: Sync
  Workflow" (`workflow_job.py`), before blueprints.

Decisions (Mike, 2026-10-20):

- A different workflow with the same name on the target is never
  overwritten, because a workflow can be local work of that appliance. The
  job changes nothing and ends WARNING, which CloudBolt's `can_be_started`
  treats as done, so the rest of the chain runs. No alias table: a workflow
  keeps one ID everywhere or is not synced.
- Steps map to plugins by global ID, else by type and name, because
  CloudBolt's hook import keeps an existing plugin's global ID when it matches
  it by name.
- Group, environment and configuration manager filters do not travel; an
  update keeps the target's own. A new workflow whose source set them, or
  whose filter names have no match, arrives disabled. "Keep each action's
  current enabled state" applies to workflows as CloudBolt applies it to
  actions.
- Attached File Archives files are not carried yet; sync accepts entries the
  target does not hold and warns. Carrying the files and finding workflows a
  blueprint pins are later phases.

### Sync (refresh the appliance from the package's home branch)

1. Group `contents` by top-level dir and map to the sync keys:
   `blueprints→blueprints`, `resource_actions→resource_actions`,
   `server_actions→server_actions`,
   `orchestration_actions→orchestration_actions`,
   `flowcontrol_actions→flow_control_actions`, `webhooks→inbound_web_hooks`,
   `recurring_jobs→recurring_action_jobs`, `cit_tests→cit_tests`,
   `mcp_tool_actions→mcp_tool_actions`, `plugins→plugins`,
   `shared_modules→shared_modules`, `extensions→extensions`. Each value is
   `"<ID>/<ID>_metadata.json"`, relative to the repo's `path_to_*`.
2. `SourceCodeRepoSyncParameters.objects.create(...)` +
   `Job.objects.create(owner=profile, job_parameters=..., type="sourcecodereposync")`,
   the same eleven lines as the OOB viewset.
3. Record `last_sync_job_id`; the list page links to the job and shows its
   status while it runs.

Member order does not matter: `sync_from_repo` processes actions, then
blueprints, and shared modules come in as dependencies. Known gap carried
over from the testing loop: a blueprint sync does not refresh an inbound
webhook its form calls, so webhooks must be explicit members. The picker
could suggest them later (see Later).

### Import from repository (create a package from a repo)

1. `repo.get_global_clone()`, `repo.fetch(repo=clone, ...)`, then
   `git ls-tree --name-only -r origin/<branch> -- packages/` filtered to
   `PKG-*/PKG-*_metadata.json`, `git show` each. Same code shape as the OOB
   metadata-file picker.
2. Register the chosen manifest locally with `local.source_code_repo_id`,
   `local.branch`, `local.remote_source_url`.
3. Open the Sync dialog prefilled. Members that do not exist yet are created
   by the sync; the package shows red pills until the job finishes.

## Repo tooling changes (this repo)

- `AGENTS.md` layout table: `packages/ | PKG- | Named sets of related content, pushed and synced together`.
- `docs/agents/metadata-schemas.md`: a `packages` section with the manifest
  schema and the member rules above.
- `tools/build_catalog.py`: add `("packages", "Packages", ...)` to `SECTIONS`
  so `CATALOG.md` and `packages/README.md` list each package with its members
  resolved to names, and each member's row in other indexes gains a
  "Packages" back-reference.
- `cb-validate-metadata`: every `contents` ref resolves, no `forms/` or
  `form_functions/` members, no duplicates, `PKG-` folder and `id` match.
- `tools/package_zip.py`: accept a `PKG-` ID and build one import zip per
  member (a bundle for appliances with no repo access). Later.
- `cb-test-content`: when the unit under test belongs to a package, sync the
  package instead of the unit, so the XUI and actions come along.

## Decisions taken in this draft

| Decision | Choice | Why |
|---|---|---|
| Members | Top-level units only, as `"<dir>/<ID>"` strings | Serializers carry the closure; the catalog and validator already parse this shape |
| Names in manifest | Not stored | Resolved live so the UI can show deleted and drifted members; README carries names for readers |
| Store | JSON file per package under `proserv/content_packages/` | No migrations, survives XUI re-sync, mirrors `remote_source_url` semantics |
| Export | Loop `push_to_remote_source` with a shared clone, then commit the manifest | Zero new git code; one commit per member is the known cost |
| Sync | Create the OOB `sourcecodereposync` job | Job log, history, same flags as the API |
| Delete | Local definition only | A package is a view over content; removing the view must not delete content |
| Access | cbadmin only | Export rewrites repo folders and sync overwrites content |

## Open questions

1. **Export as a job or inline?** The plugin-plus-`run_hook_as_job` route
   gives a log and history but makes the manager depend on a shipped plugin.
   Inline is simpler and fine while packages are small.
2. **Single commit per export?** Worth the twenty lines if PR reviewers
   object to one commit per member.
3. **Nested packages** (`includes: ["packages/PKG-..."]`) so "Platform
   baseline" can contain "EKS" and "ServiceNow". Easy in the manifest, more
   UI. Later.
4. **Suggested members.** When a blueprint is picked, scan its form for
   webhook URLs and its metadata for orchestration actions that reference it,
   and offer them. Later.
5. **API tokens.** DRF views with CloudBolt's token auth so the testing loop
   and CI can call export and sync without a session. Confirm the auth class
   first.
6. **Prefix.** `PKG-` or `CPK-`. Either is safe; `PKG-` reads better.

## First cut

1. XUI skeleton: store, admin page with the table, Add/Edit dialog with the
   picker, Delete. No git yet. Testable against a lab appliance in an afternoon.
2. Sync and Import from repository (both are job creation plus reading the
   global clone).
3. Export (per-member push, inline), then decide on job vs inline from real
   timings.
4. Repo tooling: catalog section, validator rule, schema doc, AGENTS.md row,
   and the first manifest: `packages/PKG-…` for AWS EKS.
