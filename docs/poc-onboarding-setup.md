# POC Onboarding — Setup

Get an AI agent ready to take a new CloudBolt appliance through the POC Test Plan. After these steps the agent does the rest: it reads what is configured, sends you a link for every step that needs a password or key, verifies each resource handler, reads the appliance's own logs when something fails, orders the Custom Server blueprint through MCP, and keeps the scorecard. The procedure the agent follows is [.claude/skills/cb-poc-onboarding/SKILL.md](../.claude/skills/cb-poc-onboarding/SKILL.md); agents without skill support fetch the same text with the `onb_playbook` tool.

## 1. Appliance

1. CloudBolt 2026.2.48 or later, licensed, reachable at `https://<appliance>`. The MCP server is on by default at `https://<appliance>/mcp`.
2. An account that is a CloudBolt admin (super admin or CB admin). Every onboarding tool refuses other callers.
3. Outbound HTTPS from the appliance to `github.com` and `raw.githubusercontent.com` (to fetch the toolkit and the playbook) and to `content.cloudbolt.app` (Content Library). Set the web proxy under Admin > Miscellaneous Settings if one is required.

## 2. Connect the agent

Claude Code:

```bash
claude mcp add --transport http cloudbolt https://<appliance>/mcp
```

Then `/mcp`, choose the server, and sign in to CloudBolt in the browser window as the admin. Grant the `write` scope when asked; without it no custom tool runs. Any Streamable HTTP MCP client with OAuth support connects the same way with the same URL. Check: ask "Who am I according to CloudBolt?" and the answer names your admin user.

## 3. Install the toolkit (once)

Pick one:

**A. Content Package Manager** (when the extension from this repository is installed): Admin > Content Packages > Import from repository > `https://github.com/mbomb67/cloudbolt-as-code.git`, branch `main`, package **POC Onboarding Toolkit** > Sync.

**B. Source Code Repository plus one API call.** Admin > All Admin > CloudBolt > Source Code Repositories > Add: any label, URL `https://github.com/mbomb67/cloudbolt-as-code.git`, no authentication (public repository), default paths. Note its `SCR-` id (`GET https://<appliance>/api/v3/cmp/sourceCodeRepos/`). Then:

```bash
CB_API_TOKEN=$(curl -sS -X POST "https://<appliance>/api/v3/cmp/apiToken/" \
  -H "Content-Type: application/json" \
  -d '{"username": "<admin>", "password": "<password>"}' | python -c "import json,sys; print(json.load(sys.stdin)['token'])")

curl -sS -X POST "https://<appliance>/api/v3/cmp/sourceCodeRepos/<SCR-id>/syncFromRepo/" \
  -H "Authorization: Bearer $CB_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"branch": "main", "refreshIfExists": true, "objectsToSync": {"mcp_tool_actions": [
        "MTA-6zxrhdls/MTA-6zxrhdls_metadata.json", "MTA-1985b88g/MTA-1985b88g_metadata.json"
        "MTA-zpohocjt/MTA-zpohocjt_metadata.json", "MTA-hwg74btd/MTA-hwg74btd_metadata.json"
        "MTA-jzscq0mk/MTA-jzscq0mk_metadata.json", "MTA-r5v1eblg/MTA-r5v1eblg_metadata.json"
        "MTA-92b836jb/MTA-92b836jb_metadata.json", "MTA-p72v9y76/MTA-p72v9y76_metadata.json"
        "MTA-89dxppus/MTA-89dxppus_metadata.json", "MTA-i6p4j2l7/MTA-i6p4j2l7_metadata.json"
        "MTA-iyknuxox/MTA-iyknuxox_metadata.json", "MTA-0dr8xi4i/MTA-0dr8xi4i_metadata.json"
        "MTA-i1kmy5fd/MTA-i1kmy5fd_metadata.json", "MTA-pz5o2gjl/MTA-pz5o2gjl_metadata.json"
        "MTA-70aez9bm/MTA-70aez9bm_metadata.json", "MTA-cpq7bvz4/MTA-cpq7bvz4_metadata.json"
        "MTA-tuzkb8h4/MTA-tuzkb8h4_metadata.json", "MTA-bp0rkcq9/MTA-bp0rkcq9_metadata.json"
        "MTA-0689nxjy/MTA-0689nxjy_metadata.json", "MTA-e5qv0zay/MTA-e5qv0zay_metadata.json"
        "MTA-kw8krevn/MTA-kw8krevn_metadata.json", "MTA-89w58iux/MTA-89w58iux_metadata.json"
        "MTA-qab6r2gk/MTA-qab6r2gk_metadata.json", "MTA-znqpide5/MTA-znqpide5_metadata.json"
        "MTA-ckumouj0/MTA-ckumouj0_metadata.json", "MTA-zfoxwh12/MTA-zfoxwh12_metadata.json"
        "MTA-543b19sa/MTA-543b19sa_metadata.json", "MTA-8j9c0tb0/MTA-8j9c0tb0_metadata.json"
        "MTA-ntmcnvv7/MTA-ntmcnvv7_metadata.json", "MTA-thkvyt0v/MTA-thkvyt0v_metadata.json"
        "MTA-gnx1ckno/MTA-gnx1ckno_metadata.json"]}}'
```

The response is a sync job; `GET /api/v3/cmp/jobs/<JOB-id>/` lists each tool with `success` after a few seconds. The UI alternative is Actions > MCP Tool Actions > Create from Repository, one tool at a time. The POC Server blueprint and echo webhook used in the extensibility phase are synced later by the agent (`blueprints/BP-x78p48gz`, `webhooks/IWH-f16qxnhm`).

**C. No internet from the appliance.** From a clone, `python tools/package_zip.py mcp_tool_actions/MTA-6zxrhdls` and the other folders listed in [packages/PKG-8aaadefz/README.md](../packages/PKG-8aaadefz/README.md), then Actions > MCP Tool Actions > Import for each zip.

## 4. Finish the install

Ask the agent: "Run onb_install and tell me what changed." The importer leaves Synchronous Action off on every tool; `onb_install` turns it on where it belongs, enables every tool, checks outbound access, and reports. Then reconnect the MCP client (tool lists are cached per session). Check: "Run onb_status" answers inline with the appliance inventory.

## 5. Start

Say "Onboard CloudBolt for a POC" (in Claude Code with this repository open, `/cb-poc-onboarding`). Have the [POC Test Plan](https://docs.cloudbolt.io) prerequisites at hand: the licence, the two clouds you will connect, credentials with the minimum permissions for each (the agent never asks for them; it sends you to the page where they are entered), and whether SMTP, LDAP, SSO or a DNS record for a branded portal are available. Anything that is not available is recorded as not applicable, not as a failure.

## 6. Updating the toolkit

Ask the agent to sync the package's `mcp_tool_actions/` folders from `main` (`sync_from_source_control_repo`), run `onb_install`, and reconnect. With the Content Package Manager, Sync the package instead.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A tool is missing from the agent's list | Cached tool list, or not imported | Reconnect the client; check Actions > MCP Tool Actions |
| Every custom tool fails with a scope error | Token has `read` only | Reconnect and grant `write` |
| A tool answers with a job id instead of a result | Synchronous Action off | Run `onb_install`, reconnect |
| `onb_playbook` fails | No egress to raw.githubusercontent.com | Open the `browserUrl` it returns |
| Tools refuse with "Only CloudBolt admins" | The signed-in user is not an admin | Sign in as a super admin or CB admin |
