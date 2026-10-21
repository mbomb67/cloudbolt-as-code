# POC Onboarding — Setup

Get an AI agent ready to take a new CloudBolt appliance through the POC Test Plan. After these steps the agent does the rest: it reads what is configured, sends you a link for every step that needs a password or key, verifies each resource handler, reads the appliance's own logs when something fails, orders the Custom Server blueprint through MCP, and keeps the scorecard. The procedure the agent follows is [.claude/skills/cb-poc-onboarding/SKILL.md](../.claude/skills/cb-poc-onboarding/SKILL.md); agents without skill support fetch the same text with the `onb_playbook` tool.

## 1. Appliance

1. CloudBolt 2026.2.48 or later, licensed, reachable at `https://<appliance>`. The MCP server is on by default at `https://<appliance>/mcp`.
2. An account that is a CloudBolt admin (super admin or CB admin). Every onboarding tool refuses other callers.
3. Outbound HTTPS from the appliance to `github.com` and `raw.githubusercontent.com` (to fetch the toolkit and the playbook) and to `content.cloudbolt.app` (Content Library). Set the web proxy under Admin > Miscellaneous Settings if one is required.

## 2. Appliance certificate

The appliance ships with a self-signed certificate created when the image was built. It names the build host, not your appliance, so MCP clients refuse it with `self signed certificate in certificate chain`, and with a hostname mismatch once it is trusted. Replace it before connecting, then trust it in Claude.

### Option 1: self-signed certificate for the appliance's address

The usual choice for a POC. As root on the appliance, with the IP users connect to:

```bash
IP=<appliance IP>
cp -p /etc/pki/tls/certs/localhost.crt /root/localhost.crt.bak
cp -p /etc/pki/tls/private/localhost.key /root/localhost.key.bak
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes -subj "/CN=$IP" -addext "subjectAltName=IP:$IP" -keyout /etc/pki/tls/private/localhost.key -out /etc/pki/tls/certs/localhost.crt
chown --reference=/root/localhost.crt.bak /etc/pki/tls/certs/localhost.crt /etc/pki/tls/private/localhost.key
chmod 400 /etc/pki/tls/certs/localhost.crt /etc/pki/tls/private/localhost.key
systemctl restart httpd
openssl x509 -noout -fingerprint -sha256 -in /etc/pki/tls/certs/localhost.crt
```

When users reach the appliance by a DNS name, use `subjectAltName=IP:$IP,DNS:<name>`. Note the printed SHA-256 fingerprint for the trust step. The original files stay in `/root`.

### Option 2: the customer's certificate for a DNS name

Create a DNS record for the appliance and request a certificate for that name from the customer's CA or a public CA. Put the certificate, followed by any intermediate certificates, in `/etc/pki/tls/certs/localhost.crt` and its key in `/etc/pki/tls/private/localhost.key`, then run the `chown`, `chmod` and `systemctl` lines above. Connect with `https://<dns-name>/mcp`. Claude Code reads the operating system's certificate store (native install, or npm with Node 22.15 or later), so a PC that already trusts the customer's CA skips the next step. Otherwise trust the CA's root certificate as below.

### Trust the certificate in Claude

Claude Code, in a terminal or in the Claude desktop app's Code tab, trusts the certificates in the file named by `NODE_EXTRA_CA_CERTS` and reads the variable at startup. Each block downloads the appliance's certificate, prints its SHA-256 fingerprint, and sets the variable. Compare the fingerprint with the one the appliance printed before you rely on it.

Windows (PowerShell):

```powershell
$appliance = "<appliance IP or DNS name>"
New-Item -ItemType Directory -Force "$HOME\certs" | Out-Null
$tcp = New-Object Net.Sockets.TcpClient($appliance, 443)
$ssl = New-Object Net.Security.SslStream($tcp.GetStream(), $false, ({ $true } -as [Net.Security.RemoteCertificateValidationCallback]))
$ssl.AuthenticateAsClient($appliance)
$cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
$ssl.Dispose(); $tcp.Dispose()
$b64 = [Convert]::ToBase64String($cert.RawData) -replace '(.{64})', "`$1`n"
"-----BEGIN CERTIFICATE-----`n$($b64.TrimEnd())`n-----END CERTIFICATE-----" | Set-Content -Encoding ascii "$HOME\certs\cloudbolt.crt"
[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($cert.RawData)) -replace '-', ':'
setx NODE_EXTRA_CA_CERTS "$HOME\certs\cloudbolt.crt"
# Optional: Edge and Chrome stop warning too (Windows asks you to confirm)
certutil -user -addstore Root "$HOME\certs\cloudbolt.crt"
```

macOS and Linux:

```bash
APPLIANCE="<appliance IP or DNS name>"
PROFILE=~/.zshrc   # ~/.bashrc for bash
mkdir -p ~/certs
openssl s_client -connect "$APPLIANCE:443" </dev/null 2>/dev/null | openssl x509 -out ~/certs/cloudbolt.crt
openssl x509 -in ~/certs/cloudbolt.crt -noout -fingerprint -sha256
echo 'export NODE_EXTRA_CA_CERTS="$HOME/certs/cloudbolt.crt"' >> "$PROFILE"
```

Then quit and reopen the terminal running Claude Code, or the Claude desktop app. Apps started from the Dock or a launcher may not read the shell profile; for the desktop app, or on any system, the variable can go in the `env` block of `~/.claude/settings.json` instead:

```json
{ "env": { "NODE_EXTRA_CA_CERTS": "C:/Users/<you>/certs/cloudbolt.crt" } }
```

If `NODE_EXTRA_CA_CERTS` is already set, for example for a corporate proxy, append the appliance certificate to that file instead of replacing the variable. With option 2, skip the download and point the variable at the customer's root CA certificate, and only when the PC does not already trust it. Check: `claude --debug` logs `CA certs: Appended extra certificates from NODE_EXTRA_CA_CERTS`, and `/mcp` > the server > Authenticate no longer reports a certificate error.

Other MCP clients: add the certificate to the client's trusted certificates as its documentation describes. Connectors added on claude.ai or in Claude Desktop's chat settings reach the server from Anthropic's cloud, so they need an appliance reachable from the internet with a publicly trusted certificate; use Claude Code for an appliance on a private network.

## 3. Connect the agent

Claude Code:

```bash
claude mcp add --transport http cloudbolt https://<appliance>/mcp
```

Use the address the certificate from section 2 names. Then `/mcp`, choose the server, and sign in to CloudBolt in the browser window as the admin. Grant the `write` scope when asked; without it no custom tool runs. Any Streamable HTTP MCP client with OAuth support connects the same way with the same URL. Check: ask "Who am I according to CloudBolt?" and the answer names your admin user.

## 4. Install the toolkit (once)

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
        "MTA-6zxrhdls/MTA-6zxrhdls_metadata.json", "MTA-1985b88g/MTA-1985b88g_metadata.json",
        "MTA-zpohocjt/MTA-zpohocjt_metadata.json", "MTA-hwg74btd/MTA-hwg74btd_metadata.json",
        "MTA-jzscq0mk/MTA-jzscq0mk_metadata.json", "MTA-r5v1eblg/MTA-r5v1eblg_metadata.json",
        "MTA-92b836jb/MTA-92b836jb_metadata.json", "MTA-p72v9y76/MTA-p72v9y76_metadata.json",
        "MTA-po1gmnou/MTA-po1gmnou_metadata.json", "MTA-89dxppus/MTA-89dxppus_metadata.json",
        "MTA-i6p4j2l7/MTA-i6p4j2l7_metadata.json", "MTA-iyknuxox/MTA-iyknuxox_metadata.json",
        "MTA-0dr8xi4i/MTA-0dr8xi4i_metadata.json", "MTA-i1kmy5fd/MTA-i1kmy5fd_metadata.json",
        "MTA-pz5o2gjl/MTA-pz5o2gjl_metadata.json", "MTA-70aez9bm/MTA-70aez9bm_metadata.json",
        "MTA-cpq7bvz4/MTA-cpq7bvz4_metadata.json", "MTA-tuzkb8h4/MTA-tuzkb8h4_metadata.json",
        "MTA-bp0rkcq9/MTA-bp0rkcq9_metadata.json", "MTA-0689nxjy/MTA-0689nxjy_metadata.json",
        "MTA-e5qv0zay/MTA-e5qv0zay_metadata.json", "MTA-kw8krevn/MTA-kw8krevn_metadata.json",
        "MTA-89w58iux/MTA-89w58iux_metadata.json", "MTA-qab6r2gk/MTA-qab6r2gk_metadata.json",
        "MTA-znqpide5/MTA-znqpide5_metadata.json", "MTA-ckumouj0/MTA-ckumouj0_metadata.json",
        "MTA-zfoxwh12/MTA-zfoxwh12_metadata.json", "MTA-543b19sa/MTA-543b19sa_metadata.json",
        "MTA-8j9c0tb0/MTA-8j9c0tb0_metadata.json", "MTA-ntmcnvv7/MTA-ntmcnvv7_metadata.json",
        "MTA-thkvyt0v/MTA-thkvyt0v_metadata.json", "MTA-gnx1ckno/MTA-gnx1ckno_metadata.json"]}}'
```

The response is a sync job; `GET /api/v3/cmp/jobs/<JOB-id>/` lists each tool with `success` after a few seconds. The UI alternative is Actions > MCP Tool Actions > Create from Repository, one tool at a time. The POC Server blueprint and echo webhook used in the extensibility phase are synced later by the agent (`blueprints/BP-x78p48gz`, `webhooks/IWH-f16qxnhm`).

**Testing a branch before it merges.** Use the branch name wherever these steps say `main` (option A's branch field, the `"branch"` in option B's call, section 7). The branch name must not contain a slash; CloudBolt reads it from a URL path segment. `onb_playbook` reads the skill from `main` by default: pass `onb_ref` with the branch name until it merges (`onb_install` reports when the playbook is not published on `main` yet).

**C. No internet from the appliance.** From a clone, `python tools/package_zip.py mcp_tool_actions/MTA-6zxrhdls` and the other folders listed in [packages/PKG-8aaadefz/README.md](../packages/PKG-8aaadefz/README.md), then Actions > MCP Tool Actions > Import for each zip.

## 5. Finish the install

Ask the agent: "Run onb_install and tell me what changed." The importer leaves Synchronous Action off on every tool; `onb_install` turns it on where it belongs, enables every tool, checks outbound access, and reports. Then reconnect the MCP client (tool lists are cached per session). Check: "Run onb_status" answers inline with the appliance inventory.

## 6. Start

Say "Onboard CloudBolt for a POC" (in Claude Code with this repository open, `/cb-poc-onboarding`). Have the [POC Test Plan](https://docs.cloudbolt.io) prerequisites at hand: the licence, the two clouds you will connect, credentials with the minimum permissions for each (the agent never asks for them; it sends you to the page where they are entered), and whether SMTP, LDAP, SSO or a DNS record for a branded portal are available. Anything that is not available is recorded as not applicable, not as a failure.

## 7. Updating the toolkit

Ask the agent to sync the package's `mcp_tool_actions/` folders from `main` (`sync_from_source_control_repo`), run `onb_install`, and reconnect. With the Content Package Manager, Sync the package instead.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `self signed certificate in certificate chain` when connecting | Claude does not trust the appliance's certificate | Section 2: replace it, set `NODE_EXTRA_CA_CERTS`, restart Claude |
| `does not match certificate's altnames` or `ERR_TLS_CERT_ALTNAME_INVALID` | The address in the MCP URL is not in the certificate | Regenerate it with that IP or DNS name (section 2, option 1), or connect with the name the certificate carries |
| A tool is missing from the agent's list | Cached tool list, or not imported | Reconnect the client; check Actions > MCP Tool Actions |
| Every custom tool fails with a scope error | Token has `read` only | Reconnect and grant `write` |
| A tool answers with a job id instead of a result | Synchronous Action off | Run `onb_install`, reconnect |
| `onb_playbook` fails | No egress to raw.githubusercontent.com | Open the `browserUrl` it returns |
| Tools refuse with "Only CloudBolt admins" | The signed-in user is not an admin | Sign in as a super admin or CB admin |
