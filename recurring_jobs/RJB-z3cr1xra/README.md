# URL Connectivity Check

Recurring job that probes any URLs you enter on the job and reports, per URL, which CA signed the certificate the appliance received, whether the request works under CloudBolt's current SSL setting, and, when global SSL verification is off, whether it would work with verification on. Use it to find which hosts an SSL-inspecting proxy intercepts and which CA to upload. Read-only, no shared modules.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-7ctf42u7 | URL Connectivity Check |

Schedule: `0 6 * * 1` (Mondays 06:00 appliance time), imported **disabled**. Run it on demand with **Run Now**.

## Setup
1. Admin > Recurring Jobs > URL Connectivity Check > Edit. Under the action inputs, enter the URLs to check, one per line (a bare host is checked as `https://host`), and save.
2. Run Now. The job page shows pass/fail per URL; the job log has the issuing CA, chain, trust-store contents, and fix.
3. Behind an SSL-inspecting proxy every inspected host reports the proxy's CA as issuer. Upload that CA (and any intermediate) at Admin > SSL Certificates and re-run until each URL shows OK with verification on.

## Notes
- FAILURE: a URL is unreachable under the current setting, or no URLs are set. WARNING: everything works now only because SSL verification is off. SUCCESS: all URLs OK.
- Sends anonymous GET requests only (an HTTP 401, 403, or 404 counts as reachable) and stores nothing; response bodies are not downloaded.
- `http://` URLs are probed for reachability only; there is no certificate to inspect.
- The repo ships no URL list. Enter yours on the job and re-check it after a sync of this job.
