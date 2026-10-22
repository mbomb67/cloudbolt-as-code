# Agent Activity (XUI)

Shows what AI agents did through CloudBolt's MCP server: the orders they placed or acted on, the jobs those started, and the server, resource and other history they wrote, by user, agent and MCP session, with the matching lines of CloudBolt's MCP log.

## Requirements
- CloudBolt with the MCP server (2026.2 or later).
- The MCP agent attribution patch (ps_008) on the appliance: copy it to `/var/opt/cloudbolt/proserv/patch_kit/patch_sets/`, then restart `httpd` and `jobengine`. It adds `[mcp agent="<OAuth client name>" client="<client id>" session="<12 hex>"]` to each order history, job and history record written during an MCP tool call. Records written before it was installed are not listed.

## Setup
1. Sync `extensions/XUI-y40h491l`.
2. Restart the web server (`systemctl restart httpd`): the extension adds URL paths.

## Who sees what
| Viewer | Where | Records | MCP log lines |
|---|---|---|---|
| CB Admin, Super Admin, Global Viewer | **Admin > Custom Extensions > Agent Activity**, `/xui/mcp-agent-activity/`, and the **AI Agent Activity** tab on any user's profile | Every user's | All |
| Any other user | `/xui/mcp-agent-activity/` and the **AI Agent Activity** tab on their own profile | Orders they own or acted on, jobs they own, history written under their name | Only lines logged under their username |

Global Viewers get the full view so a security team can trace agent activity without admin rights.

## Using the page
- **Filters.** User, Agent and Session lists narrow each other as you choose, and each option shows how many order, job and history rows it would list; **Search** runs the query. Dates default to the last 7 days, and a search covers at most 90. Text matches order names and numbers, job titles and types, hostnames, resource names and the marker itself, so a session id typed there finds a session the list leaves out.
- **Recent sessions.** Before a search the page lists sessions newest first, 20 per page. A session is one MCP client connection and can span many tool calls over hours or days. A user's profile tab starts on that user's sessions.
- **Session page.** Click a session id to open its own page (`/xui/mcp-agent-activity/sessions/<id>/`, breadcrumb back to Agent Activity): a one-line summary, the client app, OAuth client and client id, user, first and last seen (looking back up to 90 days), its orders, jobs and history, and its **Tool calls** from `mcp.log` with result and duration.
- **Agent and client app.** The marker names the OAuth client: the name the MCP client registered with CloudBolt (Claude Desktop and claude.ai register as Claude; `mcp-remote`, which the Claude desktop app uses to bridge HTTP servers, registers as MCP CLI Proxy whatever sits behind it). The client id is the id CloudBolt's OAuth server issued at that registration, one per installed client. Where the viewer may read the log, the page shows the application behind the connection instead (MCP `clientInfo`, e.g. `local-agent-mode-<server> (via mcp-remote 0.15.2)` shown as Claude Desktop (local agent)), with the OAuth client underneath. MCP does not report the model (Opus, Sonnet).
- **Sections.** Orders has one row per order and session, with the order events that session caused and the order's recipient. Jobs includes child jobs. Server & resource history also lists environment, group and other history. Sessions summarizes each session's counts. 20 rows per page; **CSV** exports every row of the current tab (the sessions CSV includes the client app).
- **Excel.** One `.xlsx` of everything shown: on a session page, Summary (session, client app, OAuth client and id, user, span, export time and user), Orders, Jobs, Server and resource history and Tool calls; on the list page, the same tabs plus Sessions for the current search. Written without `openpyxl` or `xlsxwriter`, which the appliance does not ship.
- **User** is the CloudBolt user whose token the agent used: the order owner for orders it placed, the approver for an approval. Records the job engine writes without an owner (an order's APPROVED, MODIFICATION and COMPLETED rows, history from a job) take the order's or job's owner, so a session has one user. Only the user is a filter; the recipient of an order placed on someone's behalf is a column. Times are appliance time.
- The same data is JSON at `/xui/mcp-agent-activity/api/` (GET; `search/`, `facets/`, `recent-sessions/`, `sections/<orders|jobs|history|sessions>/?page=N`, `sessions/<id>/log/`) with the page's query parameters: `from`, `to`, `user`, `agent`, `session`, `q`.

## Settings
`mcp_agent_activity/config.py` keeps each choice as one constant; change it, sync, restart `httpd`.

| Constant | Value | Other value |
|---|---|---|
| `VISIBILITY` | `"scoped"`: as in the table above | `"admin"`: only CB Admins, Super Admins and Global Viewers open it |
| `GLOBAL_VIEWERS_SEE_ALL` | `True` | `False`: Global Viewers see only their own records |
| `DEFAULT_VIEW` | `"recent_sessions"` | `"empty"`: nothing until Search |
| `MCP_LOG_ENABLED`, `MCP_LOG_OWN_LINES` | `True`, `True` | `False` hides the log section for everyone, or for users who see only their own records |
| `DEFAULT_DAYS`, `MAX_DAYS` | 7, 90 | |
| `FACET_MODE` | `"any"`: each filter narrows the others | `"cascade"`: user narrows agent, both narrow session |
| `RECIPIENT_IS_PERSONA` | `False`: recipient is a column | `True`: the User filter also matches an order's recipient |
| `CSV_EXPORT_ENABLED`, `XLSX_EXPORT_ENABLED` | `True`, `True` | `False` hides that export |
| `CLIENT_APP_FROM_LOG`, `KNOWN_CLIENTS` | `True`; friendly names for `clientInfo` names by prefix | `False` shows only the OAuth client name |

## Limits
- No caching: each search, filter change, page turn and CSV download reads the newest 20,000 marked records per source (order history, job messages, other history) in its dates again, and says so when it stops there.
- Tool calls and client apps come from the newest 500 matching lines in the last 64 MB of `mcp.log` on the web node serving the page. Rotated files, and other nodes of a multi-node install, are not read. A connection opened before ps_008 was loaded has no client app.
- History messages can contain user-supplied names. A name containing a marker-like string is read as one.

## Test plan (appliance with ps_008)
1. Install ps_008 and this extension as above. Connect an MCP client with `read write` scope ([docs/mcp-testing-setup.md](../../docs/mcp-testing-setup.md)).
2. Through MCP, order a blueprint that builds a server (`order_catalog_item`), approve it if it waits (`approve_order`), then run a server action on the new server (`run_server_action`).
3. As a CB Admin, open **Admin > Custom Extensions > Agent Activity**. Recent sessions lists the session: agent is the OAuth application name (for example Claude), user is the MCP user, at least one order and one job.
4. Click it. The session page shows the client app, client id and first and last seen. Orders shows CREATION and SUBMITTED (and APPROVED); Jobs shows the order job and its children; history shows the server's creation and the action. Each link opens the CloudBolt page. **Tool calls** lists the calls, and the breadcrumb returns to Agent Activity.
5. Order the same blueprint in the UI. It does not appear.
6. Choose a user: the Agent and Session lists keep only that user's. Type the hostname in Text: only that server's records remain. Choose dates 120 days apart: the search covers 90 days and says so.
7. Download each CSV. A value starting with `=` arrives prefixed with `'`.
8. As a Global Viewer, the admin page shows every user's records and all log lines.
9. As the non-admin MCP user, open your profile's **AI Agent Activity** tab and `/xui/mcp-agent-activity/`: only your records, and only your log lines. Another user's profile has no tab, and `/xui/mcp-agent-activity/api/search/` returns nobody else's records.
10. Connect a second client (another OAuth application): it appears as its own agent and session.
