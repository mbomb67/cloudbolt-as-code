---
name: cb-poc-onboarding
description: Walk a customer from a freshly deployed CloudBolt appliance to one that passes the CloudBolt POC Test Plan, over the CloudBolt MCP server. Reads status, hands the user a direct UI link for every step that involves a secret, verifies resource handlers and reads the appliance's own logs when something fails, imports environments and images, trims the order form, creates groups, users, parameters and mappings, drives the Custom Server order through MCP, records each test-plan item as done or not applicable, and reports a scorecard.
when_to_use: When the user says "onboard CloudBolt", "set up CloudBolt for a POC", "get this appliance ready", "connect my first cloud", "why did my resource handler fail", "walk me through the POC test plan", or asks for the next step on a new CloudBolt instance. Also when a CloudBolt MCP server is connected and the user asks what is configured. Not for authoring content (the cb-scaffold-* skills) or for testing content changes (cb-test-content).
---

# cb-poc-onboarding

Take a new CloudBolt appliance through the POC Test Plan with the connected CloudBolt MCP server. The procedure is tool-neutral: every step is an MCP tool call, a link the user opens, or a question. Any agent with the same MCP connection can follow it; clients without skill support get this text from the `onb_playbook` tool.

## Required reading

Before the first phase, read [references/bootstrap.md](references/bootstrap.md). Read the other references when their phase starts: [resource-handlers.md](references/resource-handlers.md) (Phase 1), [phases.md](references/phases.md) (Phases 2 to 9), [troubleshooting.md](references/troubleshooting.md) (on any failure), [test-plan-map.md](references/test-plan-map.md) (what each item needs and whether it can be skipped).

## Hard rules

1. **One ask per message.** Lead the user one step at a time. Each message carries exactly one question or one action for the user, then stops and waits for the answer. Never send a numbered list of questions, never preview the questions coming later, and never ask about a prerequisite before the step that needs it starts. Give the default inside the question ("say skip if there is none") so a one-word answer works. Keep the context to a sentence or two: what this step is for, the ask, and the link when there is one. An action that fills a form is still one ask: give the link and the values the form wants. When something finishes, say what happened in a sentence and give the next single ask. How to ask, using the richest prompt the client offers and still one question at a time:
   - **Inline form**, when the client can render one (the Claude desktop app's visual widgets, in chat and in the Code tab): a one-question form with buttons for choices, the default first, a text box for a typed value, and Skip. The answer arrives as the user's next message.
   - **Question picker**, when the client has a structured question tool (Claude Code's `AskUserQuestion`): one question per call, two to four options with the default first and marked (Recommended); a typed value goes through its free-text option.
   - **Plain text** everywhere else: one question, with choices numbered so a number answers it.

   Never put a secret in a form or a picker: the answer lands in the transcript, so secrets keep the UI link of rule 2.
2. **Secrets never pass through the agent.** Passwords, access keys, client secrets, service-account JSON, API tokens: the user enters them in the UI. Give the exact link from `onb_ui_link` (or the `editUrl` and `setPasswordUrl` fields the tools return) and list the non-secret values to type. Never ask for a secret in chat, never accept one that is pasted (say so and move on), never put one in a tool call.
3. **Root cause before retry.** When anything fails: read the tool's `error_message` and `hints`; for an order or job call `onb_failure_trace`, then `fetch_job_log` on the job it names; for a UI failure call `onb_recent_errors` for the last few minutes; then apply [troubleshooting.md](references/troubleshooting.md). State the cause and the fix before anyone retries.
4. **Status first.** Start every phase with `onb_status`. Never assume what is configured; the appliance is the source of truth.
5. **Echo and confirm every write.** Before a tool that changes the appliance or orders infrastructure, state what will change and wait for a clear yes. For `order_catalog_item` follow the server's echo-and-confirm text verbatim. `onb_remove_environment` is destructive: name the environment and get an explicit yes.
6. **Every step is skippable.** When the customer cannot or does not want to do an item (no SMTP, no LDAP, no SSO, no DNS record, no Terraform or Ansible), offer to skip it, record it with `onb_set_item_status` as `not_applicable` with the reason, and move on. The scorecard reports it as not applicable, never as failed.
7. **Record progress.** When an item's evidence exists (a successful order, a verified handler, a user who logged in), record it `done` with `onb_set_item_status`. The scorecard is built from these records plus live evidence.
8. **Scope is the test plan.** Up to two resource handlers, one or two environments each, the Custom Server and POC Server blueprints, the test-plan items. Anything more is a follow-up to propose at the end, not a step.
9. **Two response shapes.** A synchronous tool answers inline: read `outputs`. An asynchronous tool answers `{"success": true, "ids": ["JOB-..."]}`: poll `fetch_job` (or `fetch_job_log`) on that id every five seconds and read its `outputs`. The asynchronous tools are the ones that call a cloud, a directory, a mail relay or the internet: `onb_verify_resource_handler`, `onb_sync_images`, `onb_handler_locations`, `onb_import_environments`, `onb_import_os_build`, `onb_verify_ldap`, `onb_smtp_test`, `onb_install_content_library_item`, `onb_install`. Start the long ones (`onb_sync_images`) and keep working; check them before the step that needs their result. If any other tool answers with a job id, `onb_install` has not run; run it and tell the user to reconnect the client.
10. **Reconnect after install.** Tool lists are cached per client session. After `onb_install` changes anything, the user reconnects the MCP client before the next phase.
11. **Links, not hostnames.** Every URL comes from a tool. Never compose an appliance URL from memory.
12. **Nothing is pinned to a value the user has not seen.** `onb_trim_environment` pins key pairs, security groups, resource groups, datastores and subnets to one value: show the `actions` list and let the user change a pin (`onb_pin field=value`) before the first order.

## Tools

Built-in CloudBolt MCP tools used here: `fetch_identity`, `fetch_dashboard_link`, `fetch_catalog_items`, `fetch_orderable_groups`, `fetch_blueprint_schema`, `fetch_orderable_environments`, `fetch_blueprint_item_parameters`, `fetch_blueprint_parameter_options`, `order_catalog_item`, `fetch_order`, `fetch_orders_pending_my_approval`, `approve_order`, `fetch_job`, `fetch_server_list`, `fetch_server_actions`, `run_server_action`, `fetch_groups`, `fetch_group_details`, `fetch_environment_list`, `fetch_environment_details`, `set_server_power_schedule`, `fetch_server_power_schedule`.

Toolkit tools (the POC Onboarding Toolkit package; `custom_` prefix when they appear as first-class tools, otherwise `run_mcp_tool_action(mcp_tool_name, parameters)`):

| Phase | Tool | Mode | Use it to |
|---|---|---|---|
| all | `onb_status` | sync | See everything configured, recorded items and `nextSteps` with links |
| all | `onb_ui_link` | sync | Get the UI link for a global id or a named page (`new_resource_handler:aws`, `email_settings`, `user_edit:USR-...`) |
| all | `onb_recent_errors` | sync | Read the last minutes of application, jobengine, authentication or mcp log, grouped, with hints |
| all | `onb_failure_trace` | sync | Walk a failed order or job down to the job that failed, with hints and the id for `fetch_job_log` |
| all | `onb_set_item_status` | sync | Record a test-plan item or prerequisite as done, skipped, not_applicable or pending |
| 0 | `onb_install` | async | Finish the toolkit install (synchronous flags, enabled, egress checks) |
| 0 | `onb_playbook` | sync | Fetch this skill's text by section for clients without skills |
| 1 | `onb_verify_resource_handler` | async | Run Verify Connection on a handler and get the real error plus a technology hint |
| 1 | `onb_sync_images` | async | Fill the handler's image cache now without the whitelist and enable the cache job and rule with the whitelist off; start it right after verify and let it run |
| 2 | `onb_handler_locations` | async | List regions, locations or clusters (and an AWS region's VPCs) with what is already imported |
| 2 | `onb_import_environments` | async | Create environments for the chosen locations with their subnets or networks |
| 2 | `onb_import_os_build` | async | List the cached images for a location, or import an AMI, Azure image or vCenter template into an OS build attached to the environments |
| 2 | `onb_trim_environment` | sync | Shorten an environment's order form to the size (and password or network where needed) |
| 2 | `onb_configure_custom_server` | sync | Make the Custom Server blueprint orderable with a hostname template |
| 2 | `onb_remove_environment` | sync | Undo a wrong environment import |
| 4, 7 | `onb_parameter` | sync | Create a parameter and attach it to environments or groups with options and constraints |
| 4, 5 | `onb_tag_mapping` | sync | List, add or remove a handler's attribute-to-cloud-tag mappings |
| 5 | `onb_group` | sync | Create a group with entitled environments, auto-approval and a server quota |
| 5 | `onb_user` | sync | Create a local user with no password (the admin sets it on the returned link) and assign roles |
| 5 | `onb_action_approval` | sync | Turn Requires approval on or off for a server or resource action |
| 6 | `onb_verify_ldap` | async | Verify an LDAP utility and get the real bind error |
| 6 | `onb_ldap_mapping` | sync | Map a directory group DN to a CloudBolt group with roles |
| 5, 8 | `onb_smtp`, `onb_smtp_test` | sync, async | Set the non-secret mail settings (password on the returned link) and send a test |
| 8 | `onb_install_content_library_item` | async | List a Content Library collection or install one item |
| 8 | `onb_cit_from_order` | sync | Turn a finished order into a CIT test |
| 9 | `onb_scorecard` | sync | The test plan with recorded statuses, live evidence and points |
| 8 | `sync_from_source_control_repo`, `fetch_job_log`, `manage_blueprint_permissions`, `run_recurring_job`, `run_cit_tests` | sync | Reused from the testing toolkit: sync the POC Server blueprint and echo webhook, read a job's log, restore blueprint entitlements, run a recurring job or CIT test |

## The loop

Phases follow the customer's natural order: self-service first (handlers, environments, the first order, parameters), then groups, RBAC and governance, then lifecycle, then extensibility, then the wrap-up. Reorder only when a step depends on another (SMTP before approval emails, a group before a non-admin login) and say why.

For every phase:

1. `onb_status`. Tell the user in a sentence or two where things stand for this phase, then give the first ask.
2. For each step, one at a time (rule 1): if a toolkit or built-in tool does it, echo, confirm, call it, read the result. If only the UI can do it (anything with a secret, SSO, the branded portal, email templates), give the `onb_ui_link` link, the non-secret values to enter, and the documentation link from [test-plan-map.md](references/test-plan-map.md). Then verify the outcome with a tool.
3. On failure, rule 3. Stop after three attempts at one step, record the item `pending` with what is known, and continue with steps that do not depend on it.
4. Record the phase's items with `onb_set_item_status`.
5. Close the phase in a few lines: what is now configured and what was recorded (done, not applicable, pending). End with the single first ask of the next phase.

| Phase | Covers | Reference |
|---|---|---|
| 0 Bootstrap | MCP connection, admin check, toolkit install, the prerequisites the appliance already proves (each other one is asked by the step that needs it) | bootstrap.md |
| 1 Handlers | Up to two resource handlers, verified | resource-handlers.md |
| 2 Environments | One or two environments per handler, image, network, trimmed order form, hostname template | phases.md |
| 3 First order | Custom Server ordered through MCP in each environment, lifecycle actions | phases.md |
| 4 Self-service | Parameters, generated options, tags, cross-cloud visibility | phases.md |
| 5 RBAC and governance | Groups, roles, users, entitlements, approvals, quota | phases.md |
| 6 Authentication | LDAP, LDAP mapping, SSO, branded portal | phases.md |
| 7 Lifecycle | Power schedule, expiration, decommission, onboarding discovered servers | phases.md |
| 8 Extensibility | Content Library items, POC Server blueprint with scripts and generated options, webhook, recurring job, rule, CIT | phases.md |
| 9 Wrap-up | Scorecard from recorded items and live evidence | phases.md |

## Wrap-up report

`onb_scorecard` gives every test-plan item with its weight and status (done, not applicable with reason, pending), the points possible per category, and the prerequisites. Take the pending items one at a time: name the item and its evidence, ask whether it is done or not applicable, record it, then the next. Call it again, and present the final table with the evidence (order ids, job ids, object names) next to each item. The score column is the customer's judgement; the agent supplies the evidence. Then list the follow-ups the customer asked about that were out of scope.
