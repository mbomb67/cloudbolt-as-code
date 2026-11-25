# POC Onboarding Toolkit

Installs the MCP tools the POC onboarding skill drives: appliance status with next steps, admin UI links, log and failure diagnosis, resource handler verification and location import, environment creation, order-form trimming, OS build import, Custom Server setup, parameters, groups, users, action approval, tag and LDAP mappings, SMTP, the playbook text, the install step and POC item tracking, plus the repository sync, job log and blueprint permission tools they reuse.

Content package `PKG-8aaadefz`, version 0.3.0. Sync it from the Content Packages admin page (Content Package Manager UI extension). After the first import run the `onb_install` tool (asynchronous; poll the job) to turn Synchronous Action on for the inline tools, then reconnect the MCP client. Setup for a customer appliance: [docs/poc-onboarding-setup.md](../../docs/poc-onboarding-setup.md); the procedure the agent follows: [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

| Type | Name | Folder |
|---|---|---|
| MCP tool action | POC Onboarding: Install | [mcp_tool_actions/MTA-6zxrhdls](../../mcp_tool_actions/MTA-6zxrhdls/) |
| MCP tool action | POC Onboarding: Status | [mcp_tool_actions/MTA-1985b88g](../../mcp_tool_actions/MTA-1985b88g/) |
| MCP tool action | POC Onboarding: UI Link | [mcp_tool_actions/MTA-zpohocjt](../../mcp_tool_actions/MTA-zpohocjt/) |
| MCP tool action | POC Onboarding: Recent Errors | [mcp_tool_actions/MTA-hwg74btd](../../mcp_tool_actions/MTA-hwg74btd/) |
| MCP tool action | POC Onboarding: Failure Trace | [mcp_tool_actions/MTA-jzscq0mk](../../mcp_tool_actions/MTA-jzscq0mk/) |
| MCP tool action | POC Onboarding: Verify Resource Handler | [mcp_tool_actions/MTA-r5v1eblg](../../mcp_tool_actions/MTA-r5v1eblg/) |
| MCP tool action | POC Onboarding: Playbook | [mcp_tool_actions/MTA-92b836jb](../../mcp_tool_actions/MTA-92b836jb/) |
| MCP tool action | POC Onboarding: Set Item Status | [mcp_tool_actions/MTA-p72v9y76](../../mcp_tool_actions/MTA-p72v9y76/) |
| MCP tool action | POC Onboarding: Handler Locations | [mcp_tool_actions/MTA-89dxppus](../../mcp_tool_actions/MTA-89dxppus/) |
| MCP tool action | POC Onboarding: Import Environments | [mcp_tool_actions/MTA-i6p4j2l7](../../mcp_tool_actions/MTA-i6p4j2l7/) |
| MCP tool action | POC Onboarding: Trim Environment Order Form | [mcp_tool_actions/MTA-iyknuxox](../../mcp_tool_actions/MTA-iyknuxox/) |
| MCP tool action | POC Onboarding: Import OS Build | [mcp_tool_actions/MTA-0dr8xi4i](../../mcp_tool_actions/MTA-0dr8xi4i/) |
| MCP tool action | POC Onboarding: Configure Custom Server | [mcp_tool_actions/MTA-i1kmy5fd](../../mcp_tool_actions/MTA-i1kmy5fd/) |
| MCP tool action | POC Onboarding: Remove Environment | [mcp_tool_actions/MTA-pz5o2gjl](../../mcp_tool_actions/MTA-pz5o2gjl/) |
| MCP tool action | POC Onboarding: Parameter | [mcp_tool_actions/MTA-70aez9bm](../../mcp_tool_actions/MTA-70aez9bm/) |
| MCP tool action | POC Onboarding: Group | [mcp_tool_actions/MTA-cpq7bvz4](../../mcp_tool_actions/MTA-cpq7bvz4/) |
| MCP tool action | POC Onboarding: User | [mcp_tool_actions/MTA-tuzkb8h4](../../mcp_tool_actions/MTA-tuzkb8h4/) |
| MCP tool action | POC Onboarding: Action Approval | [mcp_tool_actions/MTA-bp0rkcq9](../../mcp_tool_actions/MTA-bp0rkcq9/) |
| MCP tool action | POC Onboarding: Tag Mapping | [mcp_tool_actions/MTA-0689nxjy](../../mcp_tool_actions/MTA-0689nxjy/) |
| MCP tool action | POC Onboarding: LDAP Mapping | [mcp_tool_actions/MTA-e5qv0zay](../../mcp_tool_actions/MTA-e5qv0zay/) |
| MCP tool action | POC Onboarding: Verify LDAP | [mcp_tool_actions/MTA-kw8krevn](../../mcp_tool_actions/MTA-kw8krevn/) |
| MCP tool action | POC Onboarding: SMTP Settings | [mcp_tool_actions/MTA-89w58iux](../../mcp_tool_actions/MTA-89w58iux/) |
| MCP tool action | POC Onboarding: SMTP Test | [mcp_tool_actions/MTA-qab6r2gk](../../mcp_tool_actions/MTA-qab6r2gk/) |
| MCP tool action | Sync From Source Control Repo | [mcp_tool_actions/MTA-543b19sa](../../mcp_tool_actions/MTA-543b19sa/) |
| MCP tool action | Fetch Job Log | [mcp_tool_actions/MTA-8j9c0tb0](../../mcp_tool_actions/MTA-8j9c0tb0/) |
| MCP tool action | Manage Blueprint Permissions | [mcp_tool_actions/MTA-ntmcnvv7](../../mcp_tool_actions/MTA-ntmcnvv7/) |
