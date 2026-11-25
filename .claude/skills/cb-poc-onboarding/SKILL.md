---
name: cb-poc-onboarding
description: Walk a customer from a freshly deployed CloudBolt appliance to one that passes the CloudBolt POC Test Plan, over the CloudBolt MCP server. Reads status, hands the user a direct UI link for every step that involves a secret, verifies resource handlers and reads the appliance's own logs when something fails, drives the Custom Server order through MCP, records each test-plan item as done or not applicable, and reports a scorecard.
when_to_use: When the user says "onboard CloudBolt", "set up CloudBolt for a POC", "get this appliance ready", "connect my first cloud", "why did my resource handler fail", "walk me through the POC test plan", or asks for the next step on a new CloudBolt instance. Also when a CloudBolt MCP server is connected and the user asks what is configured. Not for authoring content (the cb-scaffold-* skills) or for testing content changes (cb-test-content).
---

# cb-poc-onboarding

Take a new CloudBolt appliance through the POC Test Plan with the connected CloudBolt MCP server. The procedure is tool-neutral: every step is an MCP tool call, a link the user opens, or a question. Any agent with the same MCP connection can follow it; clients without skill support get this text from the `onb_playbook` tool.

## Required reading

Before the first phase, read [references/bootstrap.md](references/bootstrap.md). Read the other references when their phase starts: [resource-handlers.md](references/resource-handlers.md) (Phase 1), [phases.md](references/phases.md) (Phases 2 to 9), [troubleshooting.md](references/troubleshooting.md) (on any failure), [test-plan-map.md](references/test-plan-map.md) (what each item needs and whether it can be skipped).

## Hard rules

1. **Secrets never pass through the agent.** Passwords, access keys, client secrets, service-account JSON, API tokens: the user enters them in the UI. Give the exact link from `onb_ui_link` and list the non-secret values to type. Never ask for a secret in chat, never accept one that is pasted (say so and move on), never put one in a tool call.
2. **Root cause before retry.** When anything fails: read the tool's `error_message`; for an order or job call `onb_failure_trace`, then `fetch_job_log` on the job it names; for a UI failure call `onb_recent_errors` for the last few minutes; then apply [troubleshooting.md](references/troubleshooting.md). State the cause and the fix before anyone retries.
3. **Status first.** Start every phase with `onb_status`. Never assume what is configured; the appliance is the source of truth.
4. **Echo and confirm every write.** Before a tool that changes the appliance or orders infrastructure, state what will change and wait for a clear yes. For `order_catalog_item` follow the server's echo-and-confirm text verbatim.
5. **Every step is skippable.** When the customer cannot or does not want to do an item (no SMTP, no LDAP, no SSO, no DNS record, no Terraform or Ansible), offer to skip it, record it with `onb_set_item_status` as `not_applicable` with the reason, and move on. The scorecard reports it as not applicable, never as failed.
6. **Record progress.** When an item's evidence exists (a successful order, a verified handler, a user who logged in), record it `done` with `onb_set_item_status`. The scorecard is built from these records plus live evidence.
7. **Scope is the test plan.** Up to two resource handlers, one or two environments each, the Custom Server blueprint, the test-plan items. Anything more is a follow-up to propose at the end, not a step.
8. **Two response shapes.** A synchronous tool answers inline: read `outputs`. An asynchronous tool answers `{"success": true, "ids": ["JOB-..."]}`: poll `fetch_job` on that id every five seconds and read its `outputs`. `onb_verify_resource_handler` and `onb_install` are asynchronous by design. If a tool that should be synchronous answers with a job id, `onb_install` has not run; run it and tell the user to reconnect the client.
9. **Reconnect after install.** Tool lists are cached per client session. After `onb_install` changes anything, the user reconnects the MCP client before the next phase.
10. **Links, not hostnames.** Every URL comes from a tool (`onb_ui_link`, `onb_status`, the built-in `fetch_*_link` tools). Never compose an appliance URL from memory.

## Tools

Built-in CloudBolt MCP tools used here: `fetch_identity`, `fetch_dashboard_link`, `fetch_catalog_items`, `fetch_orderable_groups`, `fetch_blueprint_schema`, `fetch_orderable_environments`, `fetch_blueprint_item_parameters`, `fetch_blueprint_parameter_options`, `order_catalog_item`, `fetch_order`, `approve_order`, `fetch_job`, `fetch_server_list`, `fetch_server_actions`, `run_server_action`, `fetch_groups`, `fetch_environment_list`, `fetch_environment_details`, `set_server_power_schedule`.

Toolkit tools (the POC Onboarding Toolkit package, `custom_` prefix when they appear as first-class tools; otherwise `run_mcp_tool_action(mcp_tool_name, parameters)`):

| Tool | Mode | Use it to |
|---|---|---|
| `onb_status` | sync | See everything configured, recorded items, and `nextSteps` with links |
| `onb_ui_link` | sync | Get the UI link for a global id or a named page (`new_resource_handler:aws`, `email_settings`, `user_edit:USR-...`) |
| `onb_recent_errors` | sync | Read the last minutes of application, jobengine, authentication or mcp log, grouped, with hints |
| `onb_failure_trace` | sync | Walk a failed order or job down to the job that failed, with hints and the id for `fetch_job_log` |
| `onb_verify_resource_handler` | async | Run Verify Connection on a handler and get the real error plus a technology hint |
| `onb_playbook` | sync | Fetch this skill's text by section for clients without skills |
| `onb_install` | async | Finish the toolkit install (synchronous flags, enabled, egress checks) |
| `onb_set_item_status` | sync | Record a test-plan item or prerequisite as done, skipped, not_applicable or pending |
| `sync_from_source_control_repo`, `fetch_job_log`, `manage_blueprint_permissions` | sync | Reused from the testing toolkit: sync content, read a job's log, restore blueprint entitlements |

## The loop

Phases follow the customer's natural order: self-service first (handlers, environments, the first order, parameters), then groups, RBAC and governance, then lifecycle, then extensibility, then the wrap-up. Reorder only when a step depends on another (SMTP before approval emails, a group before a non-admin login) and say why.

For every phase:

1. `onb_status`. Summarize what exists and the `nextSteps` relevant to this phase in two or three sentences.
2. For each step: if a toolkit or built-in tool does it, echo, confirm, call it, read the result. If only the UI can do it (anything with a secret; environment import, OS builds and order-form trimming until those tools ship), give the `onb_ui_link` link, the non-secret values to enter, and the documentation link from [test-plan-map.md](references/test-plan-map.md). Then verify the outcome with a tool (`onb_verify_resource_handler`, `onb_status`, `fetch_environment_details`).
3. On failure, rule 2. Stop after three attempts at one step, record the item `pending` with what is known, and continue with steps that do not depend on it.
4. Record the phase's items with `onb_set_item_status`.
5. Close the phase with a short report: what is now configured, what was recorded (done, not applicable), what is pending, and the first step of the next phase.

| Phase | Covers | Reference |
|---|---|---|
| 0 Bootstrap | MCP connection, admin check, toolkit install, prerequisites P1 to P12 | bootstrap.md |
| 1 Handlers | Up to two resource handlers, verified | resource-handlers.md |
| 2 Environments | One or two environments per handler, image, network, trimmed order form, hostname template | phases.md |
| 3 First order | Custom Server ordered through MCP in each environment, lifecycle actions | phases.md |
| 4 Self-service | Parameters, generated options, tags, cross-cloud visibility | phases.md |
| 5 RBAC and governance | Groups, roles, users, entitlements, approvals, quota | phases.md |
| 6 Authentication | LDAP, LDAP mapping, SSO, branded portal | phases.md |
| 7 Lifecycle | Power schedule, expiration, decommission, onboarding discovered servers | phases.md |
| 8 Extensibility | Content Library items, scripts, webhook, recurring job, rule, CIT | phases.md |
| 9 Wrap-up | Scorecard from recorded items and live evidence | phases.md |

## Wrap-up report

List every test-plan item with its weight and status (done, not applicable with reason, pending), group totals, and the prerequisites. Then the follow-ups the customer asked about that were out of scope. The score column is the customer's judgement; the agent supplies the evidence (order ids, job ids, object names) next to each item.
