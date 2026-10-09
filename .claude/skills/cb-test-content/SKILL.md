---
name: cb-test-content
description: Test new or changed CloudBolt content on a live appliance through MCP before opening the PR — push to a slash-free branch, sync it with the Source Control Repos tooling, order the blueprint or run the action, poll the job, read the job log, fix and re-sync until it passes, tear down everything created, then open the PR.
when_to_use: When the user says "test this on the appliance", "sync and run it", "deploy the blueprint and check it works", "make sure the teardown works", or after any cb-scaffold-* skill once the plugin bodies are written. Also when a sync or a job on the appliance failed and the user wants it diagnosed and fixed. Not for packaging a zip (cb-package-zip) or for linting alone (cb-validate-metadata).
---

# cb-test-content

Run the full test loop for content in this repo against the connected CloudBolt MCP server: sync, exercise every function, diagnose failures from the job log, fix, re-sync, clean up, and hand back a PR.

## Required reading

Load [docs/agents/testing-loop.md](../../../docs/agents/testing-loop.md). It is the procedure; this file only says when to start it and what to collect first. For the content itself use [docs/agents/metadata-schemas.md](../../../docs/agents/metadata-schemas.md) (sync failures name its fields) and [docs/agents/plugin-templates.md](../../../docs/agents/plugin-templates.md) (entry points and return formats).

## Inputs

1. **What to test.** Default: every content folder changed on the current branch versus `origin/main`. The user may name one unit instead (`BP-...`, a name, or a folder).
2. **The appliance config** `.claude/cb-test.local.json`. If it is missing, copy `.claude/cb-test.example.json` to that path, ask the user for the values the test needs (repository, group, test server or resource), write them, and continue. Never commit it.
3. **The branch name to sync.** Must contain no slash. If the current branch has one, push the same commit as `git push origin HEAD:refs/heads/<name>` and use that name for the sync and the PR.

## Procedure

Follow testing-loop.md steps 0 to 6 in order. In short:

1. Scope: changed folders, replaced by the parents that import them. `cb-validate-metadata`, `tools/build_catalog.py`, commit, push.
2. `sync_from_source_control_repo`, poll with `fetch_job_log`, every `syncResults` row `success`.
3. Exercise by type: blueprint = order, approve if pending, discovery, every day-2 action, Delete; standalone action = run against the configured target; recurring job = `run_recurring_job`; CIT = `run_cit_tests`.
4. On failure read `fetch_job_log` (with `log_file_tail`), fix the repo, push, re-sync the affected parents, resume at the failed step. At most five iterations per unit.
5. Delete everything in the ledger; a failed delete is a teardown bug to fix and retry.
6. Open the PR from the slash-free branch with the exercised functions and job ids in the body; tell the user to sync the same paths from `main` after merging (or do it when asked).

## Constraints

- Order into any environment the MCP user may use, but choose the smallest size offered and name everything `<prefix>-<branch>-<n>` so strays are findable.
- Never end with a live test resource unreported. If one survives the cap, its id and link go first in the report.
- Confirm with the user before the first order of a session when the blueprint's schema shows a cost or quota field that looks non-trivial; otherwise proceed.
- Do not edit content on the appliance to make a test pass; every fix lands in the repo and arrives by sync.
- A testing tool that returns `{"success": true, "ids": [...]}` is running asynchronously: poll the job and tell the user to set Synchronous Action on that MCP Tool Action.
