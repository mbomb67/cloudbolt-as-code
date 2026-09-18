# Security

This repo holds sample content that CloudBolt instances sync and execute. Treat anything on `main` as code that will run on an appliance.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the **Security** tab and choose **Report a vulnerability**. Do not open a public issue for security problems. Include the content ID (for example `OHK-xxxxxxxx`), the CloudBolt version, and steps to reproduce.

Reports are acknowledged as soon as possible. This is sample content maintained on a best-effort basis, not a supported CloudBolt product, so there is no fixed response SLA.

## What is protected

- `main` accepts changes only through reviewed pull requests; force pushes and deletion are blocked.
- Every pull request runs a secret scan (gitleaks), a metadata lint that rejects instance-specific default values, a byte-compile of all Python, and a Bandit static scan. CodeQL scans Python and JavaScript.
- GitHub secret scanning and push protection are enabled.

## Using the content safely

- Pin your CloudBolt Source Code Repository to a branch or tag you have validated rather than tracking `main`.
- Review any plugin before enabling it. Plugins run with the CloudBolt service account's privileges.
- Never commit real credentials, subscription IDs, hostnames, or customer names. Use `FILL-ME` or an angle-bracket placeholder; the lint fails on GUIDs and known lab identifiers in default values.
