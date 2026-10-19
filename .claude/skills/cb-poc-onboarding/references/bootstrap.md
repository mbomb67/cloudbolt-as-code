# Phase 0: Bootstrap

Goal: the agent is connected to the appliance as a CloudBolt admin with write scope, the POC Onboarding Toolkit is installed and synchronous, and the prerequisites sheet is reviewed.

## 1. Connection

1. Call `fetch_identity`. It must return a user with `superAdmin` or `cmpAdmin` true. If it fails, the client is not connected or not authenticated: the appliance's MCP endpoint is `https://<appliance>/mcp` (no trailing slash), Streamable HTTP, OAuth 2.1 with browser login. In Claude Code: `claude mcp add --transport http cloudbolt https://<appliance>/mcp`, then `/mcp`, choose the server, authenticate as the admin. Other clients: add the same URL as a remote MCP server and complete the OAuth login.
2. The token must carry `write` scope. If every write or custom tool fails with a scope error, the client requested `read` only; reconnect and grant write when the consent page asks.
3. The API token on the user profile page and the `/api/v3/cmp/apiToken/` token do not work for `/mcp`; only the OAuth login does.
4. `fetch_dashboard_link` gives the portal base URL; confirm with the user that it is the appliance they mean.

## 2. Toolkit install (one-time manual step, then agent-driven)

Call `fetch_mcp_tool_actions`. If `onb_status` is missing, the toolkit is not installed. The sync tool cannot install itself, so the user does one of these in the UI, once:

**A. Content Package Manager** (when that extension is installed): Admin > Content Packages > Import from repository > repository `https://github.com/mbomb67/cloudbolt-as-code.git`, branch `main`, package **POC Onboarding Toolkit** (`PKG-8aaadefz`) > Sync. One step; every tool, the POC Server blueprint and the echo webhook arrive together.

**B. Source Control Repository**: Admin > All Admin > CloudBolt > Source Code Repositories > Add: a label, URL `https://github.com/mbomb67/cloudbolt-as-code.git`, no authentication (public repository), default paths. Then import the tools with the one API call in [docs/poc-onboarding-setup.md](../../../../docs/poc-onboarding-setup.md) §3, or in the UI (Actions > MCP Tool Actions > Create from Repository, one folder at a time) using the folders listed in [packages/PKG-8aaadefz/README.md](../../../../packages/PKG-8aaadefz/README.md).

**C. No internet from the appliance**: build one zip per tool with `python tools/package_zip.py mcp_tool_actions/MTA-...` from a clone and upload each at Actions > MCP Tool Actions > Import.

After the import every tool is asynchronous (the importer leaves Synchronous Action off). Run `onb_install` (asynchronous itself, so it works now): `run_mcp_tool_action("onb_install", {})`, poll the job id with `fetch_job`, read `outputs.changed`, `outputs.missing`, `outputs.egress`. Tell the user to reconnect the MCP client (tool lists are cached per session), then call `fetch_mcp_tool_actions` again: every `onb_` name from [SKILL.md](../SKILL.md) must be present. Check: `run_mcp_tool_action("onb_status", {})` answers inline.

Later toolkit updates: `sync_from_source_control_repo` with `sync_branch: main` and the package's `mcp_tool_actions/` folders as `sync_paths` (or Sync on the package), then `onb_install`, then reconnect.

## 3. First status

Call `onb_status`. Read it to the user as a short inventory: licence and days left, SMTP and proxy, Content Library reachable or not, handlers and environments, Custom Server blueprint, groups and users, LDAP or SSO, and the toolkit. Then the `nextSteps`.

## 4. Prerequisites review

Walk the prerequisites with the user and record each with `onb_set_item_status` (`done`, or `not_applicable` with a reason):

| Id | Prerequisite | Evidence or skip |
|---|---|---|
| P1 | CMP license key installed | `onb_status.license.installed` and `daysLeft` |
| P2 | Appliance deployed and initial configuration done | `fetch_identity` works |
| P3 | Up to two resource handlers, one or two environments each | Phases 1 and 2 |
| P4 | Custom Server deployed once in each environment | Phase 3 |
| P5 | Outbound 443 to content.cloudbolt.app | `onb_status.contentLibrary.reachable`; if false, `onb_install.egress` and the proxy link (`onb_ui_link proxy`); skip only if the customer accepts no Content Library items (Phase 8 shrinks) |
| P6 | Web proxy configured if required | `preferences.proxy.configured`; not applicable when no proxy is needed |
| P7 | SMTP configured | `onb_smtp` reports the settings; set the non-secret ones with it, the password on its `editUrl`, then `onb_smtp_test`. Not applicable when the POC has no relay, which also makes 2.4.1 email notification and 4.2.3, 4.2.6 partial |
| P8 | DNS A record for the portal | Needed for 2.2.1 only; not applicable otherwise |
| P9 | SSO application and an SSO SME on the call | Needed for 2.1.3 only |
| P10 | AD or LDAP service account and the DN of a mapping group | Needed for 2.1.1 and 2.1.2 |
| P11 | Ansible or Ansible Automation Platform ready | Needed for 3.2.2 and 4.3.5 (both weight 0) |
| P12 | Terraform scripts in a git repository | Needed for 4.3.4 (weight 0) |

Also confirm the two clouds for the POC (the Prerequisites sheet names POC Cloud 1 and 2) and that the user has the minimum-permission credentials for each ready, without asking what they are.

Then start Phase 1 ([resource-handlers.md](resource-handlers.md)).
