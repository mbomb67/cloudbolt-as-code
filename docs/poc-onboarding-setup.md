# POC Onboarding — Setup

Get an AI agent ready to take a new CloudBolt appliance through the POC Test Plan. After these steps the agent does the rest: it reads what is configured, sends you a link for every step that needs a password or key, verifies each resource handler, reads the appliance's own logs when something fails, orders the Custom Server blueprint through MCP, and keeps the scorecard. The procedure the agent follows is [.claude/skills/cb-poc-onboarding/SKILL.md](../.claude/skills/cb-poc-onboarding/SKILL.md); agents without skill support fetch the same text with the `onb_playbook` tool.

## 1. Appliance

1. CloudBolt 2026.2.48 or later, licensed, reachable at `https://<appliance>`. The MCP server is on by default at `https://<appliance>/mcp`.
2. An account that is a CloudBolt admin (super admin or CB admin). Every onboarding tool refuses other callers.
3. Outbound HTTPS from the appliance to `github.com` and `raw.githubusercontent.com` (to fetch the toolkit and the playbook) and to `content.cloudbolt.app` (Content Library). Set the web proxy under Admin > Miscellaneous Settings if one is required.
4. Root access to the appliance's shell, for the certificate (section 2) and one web server restart (section 4).

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

1. **Get the skill.** Clone the repository and open a terminal in it. Claude Code loads the onboarding skill from the folder it starts in.

   ```bash
   git clone https://github.com/mbomb67/cloudbolt-as-code
   cd cloudbolt-as-code
   ```

2. **Add the MCP server** with the address the certificate from section 2 names. `--scope user` makes it available from every folder; without it the server is tied to the folder you ran the command in.

   ```bash
   claude mcp add --transport http --scope user cloudbolt https://<appliance>/mcp
   ```

3. **Sign in.** Start `claude` in the `cloudbolt-as-code` folder (in the Claude desktop app, choose that folder for the Code session). Run `/mcp`, choose `cloudbolt`, **Authenticate**, and sign in to CloudBolt in the browser window as the admin. Grant the `write` scope when asked; without it no custom tool runs.

Check: ask "Who am I according to CloudBolt?" and the answer names your admin user. Any Streamable HTTP MCP client with OAuth support connects with the same URL.

## 4. Install the toolkit (once)

The toolkit installs through the Content Package Manager, a UI extension from the same repository. In the CloudBolt UI, signed in as the admin:

1. **Add the repository.** Admin > Source Code Repositories > **Add** > **GitHub**, then enter:
   - Label: `CloudBolt as Code`
   - Repository URL: `https://github.com/mbomb67/cloudbolt-as-code`
   - Auth method: Personal Access Token, with Username and Password or Token left empty, because the repository is public

   Leave the other fields at their defaults and save.
2. **Add the Content Package Manager.** Admin > Manage UI Extensions > **Add** > **Add package from Repository**. Repository `CloudBolt as Code`, branch `main`, extension JSON file `extensions/XUI-iq65xgvr/XUI-iq65xgvr_metadata.json` (the `content_packages` extension). Save.
3. **Restart the web server** as root on the appliance. The extension adds URL paths that the web server loads only at startup.

   ```bash
   systemctl restart httpd
   ```

4. **Import the package.** Admin > Admin Extensions > **Content Packages** > **Import from repository**. Choose `CloudBolt as Code` and branch `main`, select **POC Onboarding Toolkit** (`PKG-8aaadefz`) under Packages on this branch, then **Import**.
5. **Sync it.** The Sync dialog opens next. Keep the same repository and branch, check **Refresh content that already exists**, and click **Sync**. Each member syncs as its own job: the MCP tools, the POC Server blueprint and the POC Echo webhook. The package's Last sync shows the result.

## 5. Finish the install

Ask the agent: "Run onb_install and tell me what changed." The importer leaves Synchronous Action off on every tool; `onb_install` turns it on where it belongs, enables every tool, checks outbound access, and reports. Then reconnect the MCP client (tool lists are cached per session). Check: "Run onb_status" answers inline with the appliance inventory.

## 6. Start

In Claude Code, started in the `cloudbolt-as-code` folder, run `/cb-poc-onboarding` or say "Onboard CloudBolt for a POC". Have the [POC Test Plan](https://docs.cloudbolt.io) prerequisites at hand: the licence, the two clouds you will connect, credentials with the minimum permissions for each (the agent never asks for them; it sends you to the page where they are entered), and whether SMTP, LDAP, SSO or a DNS record for a branded portal are available. Anything that is not available is recorded as not applicable, not as a failure.

## 7. Updating the toolkit

Sync uses the member list saved at import, so import the package again first; that picks up tools added since.

1. Admin > Admin Extensions > Content Packages > **Import from repository**. Choose `CloudBolt as Code` and branch `main`, select **POC Onboarding Toolkit**, then **Import**. The message says the package was updated from the repository.
2. The Sync dialog opens next. Keep the same repository and branch, check **Refresh content that already exists**, and click **Sync**.
3. Ask the agent to run `onb_install`, then reconnect the MCP client so it lists the new tools.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `self signed certificate in certificate chain` when connecting | Claude does not trust the appliance's certificate | Section 2: replace it, set `NODE_EXTRA_CA_CERTS`, restart Claude |
| `does not match certificate's altnames` or `ERR_TLS_CERT_ALTNAME_INVALID` | The address in the MCP URL is not in the certificate | Regenerate it with that IP or DNS name (section 2, option 1), or connect with the name the certificate carries |
| `/mcp` does not list `cloudbolt` | The server was added without `--scope user`, from another folder | `claude mcp remove cloudbolt` in that folder, then add it again with `--scope user` (section 3) |
| `/cb-poc-onboarding` is not available | Claude Code was started outside the repository folder | Start `claude` in the `cloudbolt-as-code` folder |
| A tool is missing from the agent's list | Cached tool list, or not imported | Reconnect the client; check Actions > MCP Tool Actions |
| Content Packages page missing or returns an error after the extension sync | The web server has not loaded the extension's URL paths | `systemctl restart httpd` as root on the appliance |
| Every custom tool fails with a scope error | Token has `read` only | Reconnect and grant `write` |
| A tool answers with a job id instead of a result | Synchronous Action off | Run `onb_install`, reconnect |
| `onb_playbook` fails | No egress to raw.githubusercontent.com | Open the `browserUrl` it returns |
| Tools refuse with "Only CloudBolt admins" | The signed-in user is not an admin | Sign in as a super admin or CB admin |
