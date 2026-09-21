# CloudBolt content samples

Sample blueprints, plugins, actions, shared modules, forms, and UI extensions for CloudBolt CMP, in the folder layout that CloudBolt's **Source Control Repos** feature syncs from.

## Using this repo

1. Fork or clone it. CloudBolt treats the repo as the source of truth, so keep your copy under source control.
2. In CloudBolt, go to **Admin > All Admin > CloudBolt** and add a new Source Code Repository:

   | Field | Value |
   |---|---|
   | Label | Any name, for example `CloudBolt as Code` |
   | Repository URL | `https://github.com/mbomb67/cloudbolt-as-code.git`, or your fork |
   | Auto import branch | Leave blank to import manually. If you set one, use a branch or tag you have validated, not `main`; content on `main` changes and runs on your appliance at the next sync |
   | Path to blueprints | `blueprints/` (the default) |
   | Auth method | Personal Access Token |
   | Username / Password or Token | Your GitHub username and a token; store it locally or in a vault |

3. Pick content from [CATALOG.md](CATALOG.md) and sync it. Each content folder's README lists what syncs with it.
4. Edit every value marked `FILL-ME` or `<placeholder>` for your environment before ordering.
5. Re-enter secrets after each sync. CloudBolt redacts passwords and tokens on export, so ConnectionInfo credentials do not round-trip.
6. Validate each piece in a non-production CloudBolt instance first.

## What is here

| Directory | Contents |
|---|---|
| `blueprints/` | Orderable resources: Azure, AWS, HCP Terraform, OpenShift, Windows and Linux applications |
| `plugins/` | Python and remote-script actions called by the blueprints and actions |
| `resource_actions/`, `server_actions/` | Day-2 actions |
| `orchestration_actions/` | Lifecycle hooks: post-provision, pre-delete, rate calculation, generated options |
| `recurring_jobs/` | Scheduled jobs |
| `shared_modules/` | Reusable Python libraries |
| `forms/` | Custom order forms |
| `extensions/` | UI extensions |
| `docs/` | Setup runbooks for content that needs configuration outside CloudBolt |

[CATALOG.md](CATALOG.md) lists every piece of content with a one-line description and a link; each directory has the same table as its index README. Each content folder has a README with what it does, prerequisites, and setup steps.

## Developing content

Clone the repo to build your own content in the same layout. [docs/dev-environment-setup.md](docs/dev-environment-setup.md) covers copying the CloudBolt type stubs from your appliance and editor setup. [AGENTS.md](AGENTS.md) holds the authoring rules, which also apply if you work with an AI coding assistant; `.claude/skills/` adds Claude Code skills for scaffolding and validating content.

## Caveats

- This is sample content under the [Apache License 2.0](LICENSE). Use, copy, and modify it freely.
- It is not part of the CloudBolt product and is not covered by CloudBolt support.
- It calls cloud provider and third-party APIs (Azure, AWS, HCP Terraform, OpenShift, GitHub, Active Directory). Those APIs change, so content here can and will break over time. Test before relying on it.
- Content was exported with CloudBolt metadata version 2026.1.0. Older CloudBolt releases may not import every content type.
