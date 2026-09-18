# Development environment

How to work on this content locally with editor completion for CloudBolt's internal APIs, and how to use the bundled AI-assistant guidance.

## CloudBolt type stubs (`typings/`)

Plugins import CloudBolt's Django apps (`infrastructure.models`, `resourcehandlers.azure_arm`, `utilities.logger`, and so on). Those packages exist only on a CloudBolt appliance, so this repo expects a `typings/` folder of type stubs at its root. Editors and AI assistants read it for method signatures and model fields.

CloudBolt ships the stubs on every appliance at `/var/opt/cloudbolt/proserv/typings` and updates them with each release. Copy them from your instance:

```bash
scp -r root@<cloudbolt-host>:/var/opt/cloudbolt/proserv/typings ./typings
```

`typings/` is gitignored. Keep it out of any public fork, and copy it again after upgrading CloudBolt so the stubs match your version.

## Editor setup

- VS Code with Pylance picks up `typings/` at the workspace root automatically. The `python.analysis.stubPath` setting defaults to `typings`.
- Pyright uses the same default through `stubPath`.
- PyCharm: add `typings` to the interpreter paths (Interpreter settings, Show All, Show Interpreter Paths).

Plugins import shared modules as `shared_modules.<module_name>`. Folders in this repo are named by ID, not module name, so those imports resolve only on the appliance.

## AI assistants

[../AGENTS.md](../AGENTS.md) holds the authoring rules for this repo layout; `CLAUDE.md` points Claude Code at it. `.claude/skills/cloudbolt-content/` holds Claude Code skills that scaffold blueprints, standalone actions, forms, and Bicep blueprints, and lint metadata. [agents/](agents/) holds the deep references those rules link to. With `typings/` in place, assistants can grep real signatures instead of guessing CloudBolt APIs.
