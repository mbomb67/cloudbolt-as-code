# Bicep Engine Connectivity Check

Recurring job that probes every external host the Bicep deployment engine needs (GitHub, the Bicep release CDN, Azure login, Azure Resource Manager, optionally the module registry) and reports, per host, which CA signed the certificate the appliance received, whether the request works under CloudBolt's current SSL setting, and, when global SSL verification is off, whether it would work with verification on. Read-only.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-b21biimg | Bicep Engine Connectivity Check |
| Shared module | SHM-bbswv27r | bicep_engine |
| Shared module | SHM-eybr4hgz | github |

Schedule: `0 6 * * 1` (Mondays 06:00 appliance time), imported **disabled**. Run it on demand with **Run Now**.

## Setup
1. Nothing after sync. Admin > Recurring Jobs > Bicep Engine Connectivity Check > Run Now. The job page shows pass/fail per host; the job log has the issuing CA, chain, trust-store contents, and fix.
2. Behind an SSL-inspecting proxy every inspected host reports the proxy's CA as issuer. Upload that CA (and any intermediate) at Admin > SSL Certificates and re-run until each host shows OK with verification on.

## Notes
- FAILURE: a host is unreachable under the current setting. WARNING: everything works now only because SSL verification is off. SUCCESS: all hosts OK.
- Sends anonymous requests only (an HTTP 401 or 403 counts as reachable) and stores nothing. The binary probe transfers headers only.
- A failed order writes the same diagnosis to its job log, so the host and CA are available without running this job; the error the orderer sees stays short.
- To check URLs outside the Bicep engine's list, use the URL Connectivity Check job (RJB-z3cr1xra), which takes its URLs from the job.
