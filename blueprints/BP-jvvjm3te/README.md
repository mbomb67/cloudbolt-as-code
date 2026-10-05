# Run an Azure PowerShell Script

Ad-hoc Azure PowerShell against the subscription behind a CloudBolt Environment, with no credentials on the order form. The blueprint creates no resource: each order is one job that runs one script from the blueprint's catalog.

The catalog is the blueprint's Build tab: every **disabled** Remote Script item is a selectable script. The one enabled step, the runner plugin, mints a short-lived access token from the environment's Azure resource handler, renders the chosen Remote Script exactly as CloudBolt would, prepends a `Connect-AzAccount -AccessToken` block, and runs it on the Remote Script's own Run on Server host over WinRM.

## Contents
| Role | ID | Name |
|---|---|---|
| Build (plugin, seq 1, enabled) | OHK-pmyb1car | Run Azure PowerShell Script |
| Catalog (Remote Script, seq 2, disabled) | OHK-z9p72xu7 | Azure PS - List Resource Groups |
| Catalog (Remote Script, seq 3, disabled) | OHK-a7e4s2od | Azure PS - Tag Resource Group |
| Shared module | SHM-r0oq14r7 | env_options (RBAC-aware environment helpers) |

Order-form inputs: Environment (Azure-backed environments the group may use), Script (the catalog), Script Parameters (JSON object of the script's input values, e.g. `{"name_filter": "rg-prod-*"}`).

## Prerequisites
- An Azure resource handler whose app registration has the RBAC the scripts need (Reader for the list sample, Tag Contributor or Contributor for the tag sample), and at least one Environment on it.
- A Windows host with the Az PowerShell modules installed (`Az.Accounts`, `Az.Resources`) that CloudBolt can reach over WinRM, with its credentials stored on the server record or on each Remote Script.
- Azure public cloud. The sign-in block does not pass `-Environment`, so sovereign clouds need a one-line change in the runner.

## Setup
1. After sync, open each catalog Remote Script and set **Run on Server** to the Az PowerShell host. This binding is instance-specific and is not carried by the repo; the runner fails with a clear message when it is missing.
2. Confirm the run-as credential on each Remote Script, or leave it blank to use the server's own credentials. The exported `credentials` value is the redacted placeholder `YOUR_CREDENTIALS`.
3. Grant the blueprint to the groups that may run scripts. Environment entitlement is evaluated per order from `group.get_available_environments()`.

## Adding a script
1. Add the `.ps1` as a Remote Script build item on this blueprint, Windows OS family, and **untick Enabled**. Keep it disabled: an enabled item would run on every order.
2. Declare its inputs as action inputs and reference them with the usual template variables; the runner passes the order's Script Parameters through CloudBolt's own rendering, so PWD inputs are still masked and pinned item defaults still apply.
3. Do not call `Connect-AzAccount`; the session is already signed in to the selected environment's subscription. Use `Set-AzContext` to switch subscriptions within the tenant. Extra template variables available: `cb_azure_subscription_id`, `cb_azure_tenant_id`, `cb_azure_location`, `cb_target_environment`. The standard server, environment and group variables resolve to the Run on Server host, as for any Remote Script that uses one.

## Notes
- Access tokens last 60 to 90 minutes by default (tenant policy can shorten this); the remaining lifetime is logged when the job starts. Scripts that run longer need the client-secret sign-in instead.
- The token travels only inside the script body over WinRM and is removed from the PowerShell session right after sign-in; `-Scope Process` keeps the Az context out of the host's user profile. Nothing logs the rendered script. Script output is written to the job log, so scripts should not print secrets.
- Each run leaves a MODIFICATION event on the Run on Server host, as a native Remote Script run does.
- Value substitution is textual, as for any Remote Script: quote inputs in the script (`'{{ name }}'`) and keep inputs that feed PowerShell literals constrained with options or a regex.
- A custom order form that lists the chosen script's inputs dynamically is a planned follow-up; until then Script Parameters is a JSON text field.
