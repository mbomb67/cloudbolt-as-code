# Bicep Deployment

Deploys infrastructure from an Azure Bicep template hosted in GitHub as an Azure deployment stack. CloudBolt fetches the template, compiles it with a self-bootstrapping Bicep CLI, shows a what-if preview, pauses for approval, then deploys. This is the generic engine blueprint; its defaults point at the sample storage-account template in this repo.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-gqvi9kv4 | Deploy Bicep Template |
| Teardown | OHK-t2gs5caq | Teardown Bicep Deployment |
| Day-2 action | RSA-fa7r7cg7 | Update Bicep Deployment |
| Day-2 action | RSA-5jeixn92 | Drift Check |
| Shared module | SHM-eybr4hgz | github |
| Shared module | SHM-bbswv27r | bicep_engine |
| Form | FRM-84n18crj | Bicep Deployment |

## Prerequisites
- An Azure Resource Manager resource handler and at least one Environment on it. The order form exposes only the Environment; the handler is derived from it and never shown.
- Handler service principal with `Microsoft.Resources/deploymentStacks/*`, `Microsoft.Resources/deployments/*` (what-if) and rights to the resource types the template deploys. Contributor on the target resource group covers this.
- A ConnectionInfo named exactly `GitHub` (protocol https, host `api.github.com`, port 443) with a read-only token in the password field. Required even for public repositories; the engine always authenticates.
- Appliance: jobengine workers must be able to execute a downloaded binary under `PROSERV_DIR` (no `noexec` mount or SELinux block), with outbound HTTPS to GitHub releases, `api.github.com`, `management.azure.com` and `login.microsoftonline.com`.

## Setup
1. Follow the runbook: [../../docs/bicep-deployment-setup.md](../../docs/bicep-deployment-setup.md).
2. Re-enter the `GitHub` ConnectionInfo token after every repo sync; CloudBolt redacts it on export.
3. The deployment item defaults are Repository `mbomb67/cloudbolt-as-code`, Ref `main`, Template Path `docs/examples/bicep/storage-account/main.bicep`. Change them (in the blueprint's parameter defaults and the hidden fields of FRM-84n18crj) to your own template, and pin a tag or commit SHA for anything beyond a demo.
4. The form's "Input Parameters" panel is hand-built for the sample template (`storageAccountName`, `sku`, `accessTier`). If you point at a different template, edit that panel or use the `scaffold-bicep` skill to generate a typed blueprint.
5. Review the config block at the top of `shared_modules/SHM-bbswv27r/SHM-bbswv27r_script.py` (pinned Bicep version and SHA256, download URL or mirror, deny settings). Shared modules are cached in the running process; restart CloudBolt after changing one.

## Notes
- Build and Update pause after the what-if preview. Approve with "Continue Job" (cb_admin only); cancelling the job rejects. Nothing is submitted to Azure before approval. The approval window is the global `job_timeout` preference (default 8h).
- Target scope is detected from the compiled template, not from the form. Resource-group-scoped templates need the Resource Group field; subscription-scoped templates ignore it.
- Teardown deletes the stack with delete-all semantics. It is idempotent and tolerates failed builds (missing stack yields WARNING) but never force-deletes; a 409 from locked or externally managed resources fails with guidance.
- Drift Check is read-only and never pauses. Update never edits pinned parameters; do not paste `@secure()` values into the plaintext Parameters (JSON) field.
- Sample template and walkthrough: [../../docs/examples/bicep/storage-account/README.md](../../docs/examples/bicep/storage-account/README.md).
