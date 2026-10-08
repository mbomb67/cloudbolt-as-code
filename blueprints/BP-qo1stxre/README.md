# Run an Azure PowerShell Script (Deployment Script)

Ad-hoc Azure PowerShell against the subscription behind a CloudBolt Environment, with no credentials on the order form and **no server to run it on**. Each order runs one script from the blueprint's catalog in a container that Azure starts for that run, through an [ARM deployment script](https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/deployment-script-template). The blueprint creates no resource and never uses a Run on Server host.

## Which blueprint to use

This repo ships two blueprints with the same script catalog model and the same order form; they differ only in where the script executes. Sync the one that fits, or both.

| | [Run an Azure PowerShell Script](../BP-jvvjm3te/README.md) (BP-jvvjm3te) | Run an Azure PowerShell Script (Deployment Script), this blueprint (BP-qo1stxre) |
|---|---|---|
| Execution host | A Windows host you manage, set as each Remote Script's Run on Server, reached over WinRM or Azure Run Command | None. A Microsoft-managed Linux container (Azure Container Instances) that Azure creates for the run and removes afterwards |
| What you maintain | The host and its machine-wide Az modules | Nothing but a resource group per subscription |
| PowerShell | Whatever the host has (Windows PowerShell 5.1 under Run Command) | PowerShell 7 with the Az modules of the pinned image |
| Time to first output | Seconds | A few minutes: Azure provisions a storage account and a container for every run |
| Per-run Azure cost | None | Seconds of container time plus a transient storage account |
| Permissions | Host credentials | The app registration creates and deletes deployment scripts, storage accounts and container instances in the host resource group |
| Scripts | Windows-only cmdlets work | Pure Az PowerShell only |

## How it works

The one enabled step mints a short-lived access token from the environment's Azure resource handler, renders the chosen Remote Script exactly as CloudBolt would, submits it as a `Microsoft.Resources/deploymentScripts` resource (kind AzurePowerShell) with the token in a secure environment variable, polls it, writes the container's output to the job and deletes the resource. Azure removes the temporary storage account and container itself. Everything is ARM REST from the appliance; nothing is installed on it.

## Contents
| Role | ID | Name |
|---|---|---|
| Build (plugin, seq 1, enabled) | OHK-oblhxhw2 | Run Azure PowerShell Script (Deployment Script) |
| Catalog (Remote Script, seq 2, disabled) | OHK-kun5r55v | Azure PS (Deployment Script) - List Resource Groups |
| Catalog (Remote Script, seq 3, disabled) | OHK-pmng6q6r | Azure PS (Deployment Script) - Tag Resource Group |
| Custom form | FRM-qfupiiyj | Run an Azure PowerShell Script (Deployment Script) |
| Form function | FJS-qfhsd08t | azdsBuildScriptPanel |
| Inbound webhook (shared with BP-jvvjm3te) | IWH-vfkvduxm | Azure PS Script Panel (`/api/v3/cmp/inboundWebHooks/azps-script-panel/run/`) |
| Webhook plugin | OHK-bvn2l7q1 | Azure PS Script Panel |
| Shared module | SHM-r0oq14r7 | env_options (RBAC-aware environment helpers) |

## Prerequisites
- An Azure resource handler whose app registration has the RBAC the scripts need (Reader for the list sample, Tag Contributor or Contributor for the tag sample) **and** can create and delete deployment scripts, storage accounts and container instances in the host resource group. Contributor on that group is the simple choice; Microsoft documents a least-privilege custom role in the deployment-script article above. The runner creates the group when it is missing, which needs that right at subscription scope; otherwise create it by hand.
- Each Environment's location set to a region where Azure Container Instances is available. The container runs there.
- The `Microsoft.Storage` and `Microsoft.ContainerInstance` resource providers registered in the subscription.
- Azure public cloud. The sign-in block does not pass `-Environment`, so sovereign clouds need a one-line change in the runner.

## Setup
1. Sync the blueprint, then sync `webhooks/` separately: a blueprint sync imports the form and form function but does not refresh the inbound webhook the form calls. The webhook is the one BP-jvvjm3te uses; syncing it once serves both.
2. Optional: change the host resource group. The default `cloudbolt-deployment-scripts` is pinned in the form as the hidden question `plugin-bdi-ohh6grk9.script_resource_group`; edit its `defaultValue`.
3. Optional: change the Az module version, `AZ_POWERSHELL_VERSION` in the runner. Supported values are the tags of the [azuredeploymentscripts-powershell image](https://mcr.microsoft.com/v2/azuredeploymentscripts-powershell/tags/list) without the `az` prefix.
4. Grant the blueprint to the groups that may run scripts. Environment entitlement is evaluated per order from `group.get_available_environments()`, and the webhook only answers for groups that may deploy the blueprint.

## Adding a script
Same as BP-jvvjm3te: add the `.ps1` as a Remote Script build item, **untick Enabled**, declare its inputs as action inputs, and do not call `Connect-AzAccount`. Differences:
- The script runs under PowerShell 7 on Linux, so Windows-only cmdlets are unavailable; line endings are normalised for you. There is no Run on Server to set and the item's OS family is informational.
- Extra template variables: `cb_azure_subscription_id`, `cb_azure_tenant_id`, `cb_azure_location`, `cb_target_environment`, `cb_script_resource_group`. There is no server in the context, so server variables do not resolve.
- Command-line arguments on the Remote Script are passed as the deployment script's `arguments`.

## Notes
- Azure needs one to three minutes to provision the storage account and container before the script starts; the job reports each state. The Remote Script's execution timeout bounds the container run (one day at most) and the job waits 15 minutes longer than that before giving up.
- Access tokens last 60 to 90 minutes by default; keep the execution timeout below that. Scripts that must run longer need a user-assigned managed identity on the deployment script resource, which this runner does not implement.
- The token travels only as a secure environment variable, which Azure never returns when the resource is read, and the wrapper removes it from the environment right after sign-in. Nothing logs the rendered script. Script output is written to the job log, so scripts should not print secrets.
- Failures: the wrapper traps terminating errors into `CB_ERROR:` lines and exits 1, which Azure reports as a Failed deployment script; a `CB_INFO:` line confirms the Az.Accounts version and subscription, and a `CB_WARNING:` block lists non-terminating errors. The job's error field shows Azure's error plus the output tail; the full output is behind the job's output link.
- Nothing is left behind: the deployment script resource is deleted after its output is read, and Azure deletes the storage account and container (`cleanupPreference` Always). A run CloudBolt could not wait out stays visible in the portal and expires an hour after it finishes. Resources carry the tags `cloudbolt_job`, `cloudbolt_blueprint` and `cloudbolt_script`.
- Form limits and troubleshooting are the same as BP-jvvjm3te (webhook timings under `azps-script-panel` in `application.log`).
- The form's hidden `blueprint_id`, `custom_form_id` and build-item question names (`plugin-bdi-ohh6grk9.*`) are tied to this blueprint's IDs; a copy of the blueprint needs its own form.
