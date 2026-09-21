# Development environment

How to work on this content locally with editor completion for CloudBolt's internal APIs, and how to use the bundled AI-assistant guidance.

## CloudBolt type stubs (`typings/`)

Plugins import CloudBolt's Django apps (`infrastructure.models`, `resourcehandlers.azure_arm`, `utilities.logger`, and so on). Those packages exist only on a CloudBolt appliance, so this repo expects a `typings/` folder of type stubs at its root. Editors and AI assistants read it for method signatures and model fields.

CloudBolt ships the stubs on every appliance at `/var/opt/cloudbolt/proserv/typings` and updates them with each release. That path is a symlink, so archive it with a command that follows links, then download and extract the archive in the repo root.

1. On the appliance, create the archive.

   Mac / Linux client:

   ```bash
   tar -chzf /tmp/cloudbolt-typings.tar.gz -C /var/opt/cloudbolt/proserv typings/
   ```

   Windows client:

   ```bash
   cd /var/opt/cloudbolt/proserv && zip -r /tmp/cloudbolt-typings.zip typings/
   ```

2. From the repo root on your machine, download it.

   Mac / Linux:

   ```bash
   scp root@<cloudbolt-host>:/tmp/cloudbolt-typings.tar.gz ./cloudbolt-typings.tar.gz
   ```

   Windows (PowerShell):

   ```powershell
   scp root@<cloudbolt-host>:/tmp/cloudbolt-typings.zip ./cloudbolt-typings.zip
   ```

3. Extract it and delete the archive.

   Mac / Linux:

   ```bash
   tar -xzf cloudbolt-typings.tar.gz && rm cloudbolt-typings.tar.gz
   ```

   Windows (PowerShell):

   ```powershell
   Expand-Archive cloudbolt-typings.zip . ; Remove-Item cloudbolt-typings.zip
   ```

This creates `typings/` in the repo root. It is gitignored. Keep it out of any public fork, and repeat these steps after upgrading CloudBolt so the stubs match your version.

## Editor setup

- VS Code: install [CloudBolt Content Navigator](https://marketplace.visualstudio.com/items?itemName=CloudBoltSoftware.cloudbolt-vscode-plugin) (`CloudBoltSoftware.cloudbolt-vscode-plugin`). It shows the repo's content as a tree by human-readable name and type, expands each blueprint into its deployment items, management actions, teardown items, and discovery plugin, and can sync changes to a CloudBolt instance. It is the in-editor counterpart of [../CATALOG.md](../CATALOG.md).
- VS Code with Pylance picks up `typings/` at the workspace root automatically. The `python.analysis.stubPath` setting defaults to `typings`.
- Pyright uses the same default through `stubPath`.
- PyCharm: add `typings` to the interpreter paths (Interpreter settings, Show All, Show Interpreter Paths).

Plugins import shared modules as `shared_modules.<module_name>`. Folders in this repo are named by ID, not module name, so those imports resolve only on the appliance.

## AI assistants

[../AGENTS.md](../AGENTS.md) holds the authoring rules for this repo layout; `CLAUDE.md` points Claude Code at it. `.claude/skills/cloudbolt-content/` holds Claude Code skills that scaffold blueprints, standalone actions, forms, and Bicep blueprints, and lint metadata. [agents/](agents/) holds the deep references those rules link to. With `typings/` in place, assistants can grep real signatures instead of guessing CloudBolt APIs.
