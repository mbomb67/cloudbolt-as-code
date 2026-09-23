# Docs

Setup runbooks for content that needs configuration outside CloudBolt. Each content README links to its runbook.

| Runbook | Content |
|---|---|
| [ad-dns-ldap-setup.md](ad-dns-ldap-setup.md) | DNS A Record blueprint (`BP-dnsrec01`); AD DNS create/delete hooks (`HPA-dnscrt01`, `HPA-dnsdec01`) |
| [azure-negotiated-rate-setup.md](azure-negotiated-rate-setup.md) | Azure Resource Manager Rate Hook (`HPA-t7hlyvyy`); Azure Price Sheet Refresh (`RJB-reblryol`) |
| [azure-vm-disk-encryption-setup.md](azure-vm-disk-encryption-setup.md) | Azure CMK disk encryption hooks (`HPA-w1dmx20b`, `HPA-h7g0i0dx`) |
| [bicep-deployment-setup.md](bicep-deployment-setup.md) | Bicep Deployment (`BP-nibk4erf`); Azure Resource Group - Bicep (`BP-p7zmh96m`) |
| [hcp-terraform-setup.md](hcp-terraform-setup.md) | HCP Terraform VM (`BP-b0qm83lh`); Form Options webhook (`IWH-yj93is5z`); `env_options` shared module (`SHM-r0oq14r7`) |
| [hcp-no-code-setup.md](hcp-no-code-setup.md) | HCP Terraform No-Code Module (`BP-00meiwwz`) |
| [linux-ad-domain-join-runbook.md](linux-ad-domain-join-runbook.md) | Join Linux Server to AD Domain (`HPA-o6ctckmt`) |
| [windows-ca-cert-request-setup.md](windows-ca-cert-request-setup.md) | Request Certificate (Windows CA) (`BP-lt6a3yzf`) |

Other folders:

- `examples/bicep/` — the sample Bicep template the Bicep Deployment blueprint points at by default.
- `fixtures/` — how to capture a byte-match fixture for the AD DNS record encoder before trusting it in production.
- [dev-environment-setup.md](dev-environment-setup.md) — copying the CloudBolt type stubs from your appliance and setting up an editor to develop content.
- `agents/` — authoring conventions for AI coding assistants working in this repo layout. See [../AGENTS.md](../AGENTS.md).
