# Run an Azure PowerShell Script

Ad-hoc Azure PowerShell against the subscription behind a CloudBolt Environment, with no credentials on the order form. The blueprint creates no resource: each order is one job that runs one script from the blueprint's catalog.

The catalog is the blueprint's Build tab: every **disabled** Remote Script item is a selectable script. The one enabled step, the runner plugin, mints a short-lived access token from the environment's Azure resource handler, renders the chosen Remote Script exactly as CloudBolt would, prepends a `Connect-AzAccount -AccessToken` block, and runs it on the Remote Script's own Run on Server host over WinRM.

The custom order form asks for Group, Environment and Script, then rebuilds its Script Parameters panel from the chosen script's declared action inputs (types, required flags, options, min/max and regex constraints) each time the selection changes.

## Contents
| Role | ID | Name |
|---|---|---|
| Build (plugin, seq 1, enabled) | OHK-pmyb1car | Run Azure PowerShell Script |
| Catalog (Remote Script, seq 2, disabled) | OHK-z9p72xu7 | Azure PS - List Resource Groups |
| Catalog (Remote Script, seq 3, disabled) | OHK-a7e4s2od | Azure PS - Tag Resource Group |
| Custom form | FRM-mfveruw6 | Run an Azure PowerShell Script |
| Form function | FJS-ouuq5zsq | azpsBuildScriptPanel |
| Inbound webhook | IWH-vfkvduxm | Azure PS Script Panel (`/api/v3/cmp/inboundWebHooks/azps-script-panel/run/`) |
| Webhook plugin | OHK-bvn2l7q1 | Azure PS Script Panel |
| Shared module | SHM-r0oq14r7 | env_options (RBAC-aware environment helpers) |

## Prerequisites
- An Azure resource handler whose app registration has the RBAC the scripts need (Reader for the list sample, Tag Contributor or Contributor for the tag sample), and at least one Environment on it.
- A Windows host with the Az PowerShell modules installed **machine-wide** (`Install-Module Az -Scope AllUsers`; at least `Az.Accounts` and `Az.Resources`). On an Azure VM CloudBolt runs the script through Azure Run Command, which executes as SYSTEM under Windows PowerShell 5.1 and cannot see modules installed for a single user. Elsewhere it uses WinRM with the credentials stored on the server record or on each Remote Script.
- Azure public cloud. The sign-in block does not pass `-Environment`, so sovereign clouds need a one-line change in the runner.

## Setup
1. Sync the blueprint, then sync `webhooks/` separately: a blueprint sync imports the form and form function but does not refresh the inbound webhook the form calls.
2. Open each catalog Remote Script and set **Run on Server** to the Az PowerShell host. This binding is instance-specific and is not carried by the repo; the runner fails with a clear message when it is missing.
3. Confirm the run-as credential on each Remote Script, or leave it blank to use the server's own credentials. The exported `credentials` value is the redacted placeholder `YOUR_CREDENTIALS`.
4. Grant the blueprint to the groups that may run scripts. Environment entitlement is evaluated per order from `group.get_available_environments()`, and the webhook only answers for groups that may deploy the blueprint.

## Adding a script
1. Add the `.ps1` as a Remote Script build item on this blueprint, Windows OS family, and **untick Enabled**. Keep it disabled: an enabled item would run on every order.
2. Declare its inputs as action inputs and reference them with the usual template variables. The form renders them from that declaration, and the runner passes the submitted values through CloudBolt's own rendering, so PWD inputs are still masked and pinned item defaults still apply (a pinned input is hidden from the form).
3. Do not call `Connect-AzAccount`; the session is already signed in to the selected environment's subscription. Use `Set-AzContext` to switch subscriptions within the tenant. Extra template variables available: `cb_azure_subscription_id`, `cb_azure_tenant_id`, `cb_azure_location`, `cb_target_environment`. The standard server, environment and group variables resolve to the Run on Server host, as for any Remote Script that uses one.

## Notes
- Access tokens last 60 to 90 minutes by default (tenant policy can shorten this); the remaining lifetime is logged when the job starts. Scripts that run longer need the client-secret sign-in instead.
- The token travels only inside the script body over WinRM and is removed from the PowerShell session right after sign-in; `-Scope Process` keeps the Az context out of the host's user profile. Nothing logs the rendered script. Script output is written to the job log, so scripts should not print secrets.
- Each run leaves a MODIFICATION event on the Run on Server host, as a native Remote Script run does.
- Failures: CloudBolt sees only what the script prints plus its exit code. The wrapper traps any terminating error, including a failed sign-in or a missing Az module, prints it as a `CB_ERROR:` line with the position and stack, and exits 1; a `CB_INFO:` line confirms which Az.Accounts version connected to which subscription, and a `CB_WARNING:` block at the end lists non-terminating errors the script left behind. The job's error field shows that output; the full stream is behind the job's output link, and the Python traceback for anything that failed before PowerShell ran is in `application.log`. Scripts should still print their own errors with `Write-Output` and `exit 1`, as the two samples do.
- Value substitution is textual, as for any Remote Script: quote inputs in the script (`'{{ name }}'`) and keep inputs that feed PowerShell literals constrained with options or a regex.
- Troubleshooting the form: every webhook call logs its steps and timings to `application.log` under `azps-script-panel <script id>`, and the form gives up on a call after 45 seconds with an error that says so.
- Form limits in this version: show/hide dependencies between inputs are not rendered, and an input whose options depend on another input renders as a text box with a hint. Inputs typed TXT or CODE render as multi-line text; PWD and ETXT as password fields.
- The form's hidden `blueprint_id`, `custom_form_id` and build-item question names (`plugin-bdi-9c6bb45b.*`) are tied to this blueprint's IDs; a copy of the blueprint needs its own form.
