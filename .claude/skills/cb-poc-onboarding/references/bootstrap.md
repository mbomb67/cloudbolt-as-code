# Phase 0: Bootstrap

Goal: the agent is connected to the appliance as a CloudBolt admin with write scope, the POC Onboarding Toolkit is installed and synchronous, and the prerequisites the appliance already proves are recorded.

## 1. Connection

1. Call `fetch_identity`. It must return a user with `superAdmin` or `cmpAdmin` true. If it fails, the client is not connected or not authenticated: the appliance's MCP endpoint is `https://<appliance>/mcp` (no trailing slash), Streamable HTTP, OAuth 2.1 with browser login. In Claude Code: `claude mcp add --transport http --scope user cloudbolt https://<appliance>/mcp`, then `/mcp`, choose the server, authenticate as the admin. Without `--scope user` the server is tied to the folder the command ran in and is missing from `/mcp` elsewhere: re-add it with `--scope user`. Other clients: add the same URL as a remote MCP server and complete the OAuth login. A TLS error (`self signed certificate in certificate chain`, a certificate altnames mismatch) means the appliance certificate is not trusted yet; the agent cannot fix it over MCP. Send the user to section 2 of [docs/poc-onboarding-setup.md](../../../../docs/poc-onboarding-setup.md): replace the certificate as root on the appliance, set `NODE_EXTRA_CA_CERTS`, restart the client.
2. The token must carry `write` scope. If every write or custom tool fails with a scope error, the client requested `read` only; reconnect and grant write when the consent page asks.
3. The API token on the user profile page and the `/api/v3/cmp/apiToken/` token do not work for `/mcp`; only the OAuth login does.
4. `fetch_dashboard_link` gives the portal base URL; confirm with the user that it is the appliance they mean.

## 2. Toolkit install (one-time manual step, then agent-driven)

Call `fetch_mcp_tool_actions`. If `onb_status` is missing, the toolkit is not installed. The sync tool cannot install itself, so the user installs it once in the UI through the Content Package Manager. Walk them through section 4 of [docs/poc-onboarding-setup.md](../../../../docs/poc-onboarding-setup.md): add the Source Code Repository `https://github.com/mbomb67/cloudbolt-as-code` (GitHub, no credentials), add the UI extension `extensions/XUI-iq65xgvr` from it, restart httpd as root, then Import from repository and Sync the package **POC Onboarding Toolkit** (`PKG-8aaadefz`). Every tool, the POC Server blueprint and the echo webhook arrive together. If the Content Packages page is missing after the extension sync, httpd has not been restarted.

After the import every tool is asynchronous (the importer leaves Synchronous Action off). Run `onb_install` (asynchronous itself, so it works now): `run_mcp_tool_action("onb_install", {})`, poll the job id with `fetch_job`, read `outputs.changed`, `outputs.missing`, `outputs.egress`. Tell the user to reconnect the MCP client (tool lists are cached per session), then call `fetch_mcp_tool_actions` again: every `onb_` name from [SKILL.md](../SKILL.md) must be present. Check: `run_mcp_tool_action("onb_status", {})` answers inline.

Later toolkit updates: the user clicks Sync on the package (Admin > Admin Extensions > Content Packages), then `onb_install`, then reconnect.

## 3. First status

Call `onb_status`. Tell the user in two sentences what is already in place and what comes next. Do not read the whole inventory unless they ask for it.

## 4. Prerequisites

Record what the appliance already proves, without asking, and mention it in one sentence: P1 from `onb_status.license`, P2 because `fetch_identity` works, P5 from `onb_status.contentLibrary.reachable`, P6 from `preferences.proxy` (not applicable when no proxy is needed). When P5 is false, that becomes the next single ask: the proxy link (`onb_ui_link proxy`), or skip, which shrinks Phase 8.

Ask nothing else up front. Each remaining prerequisite is one question, asked when the step that needs it starts (rule 1 in [SKILL.md](../SKILL.md)), and recorded with `onb_set_item_status` right after the answer:

| Id | Prerequisite | Asked or proven at |
|---|---|---|
| P3 | One or two resource handlers, one or two environments each | Phase 1 asks for the first cloud, and for a second only after the first is verified; Phase 2 records it |
| P4 | Custom Server deployed once in each environment | Phase 3, from the order |
| P7 | SMTP relay | Phase 5, before the first approval email |
| P8 | DNS record for a branded portal | Phase 6, branded portal step |
| P9 | SSO application and an SSO SME | Phase 6, SSO step |
| P10 | AD or LDAP service account and a mapping group DN | Phase 6, LDAP step |
| P11, P12 | Ansible, Terraform in a git repository | Phase 8, last step (both weight 0) |

Then start Phase 1 ([resource-handlers.md](resource-handlers.md)) with its first ask.
