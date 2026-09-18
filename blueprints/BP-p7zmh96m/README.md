# Azure Resource Group - Bicep

Creates an Azure resource group by deploying Microsoft's public `subscription-deployments/create-rg/main.bicep` template (Azure/azure-quickstart-templates) as a subscription-scoped Azure deployment stack. The orderer picks an Environment, a name and a location; an optional second step offers the new resource group in that Environment's Resource Group dropdown for VM orders. Built on the same engine plugins as the generic Bicep Deployment blueprint (BP-nibk4erf).

## Contents
| Role | ID | Name |
|---|---|---|
| Build (seq 1) | OHK-gqvi9kv4 | Deploy Bicep Template |
| Build (seq 2, optional) | OHK-r1imfgdx | Add Resource Group to Environment |
| Teardown | OHK-vm5p34w3 | Remove Resource Group from Environments |
| Teardown | OHK-t2gs5caq | Teardown Bicep Deployment |
| Day-2 action | RSA-fa7r7cg7 | Update Bicep Deployment |
| Day-2 action | RSA-5jeixn92 | Drift Check |
| Shared module | SHM-eybr4hgz | github |
| Shared module | SHM-bbswv27r | bicep_engine |
| Form | FRM-i9zadhpc | Azure Resource Group - Bicep |

## Prerequisites
- Everything the Bicep Deployment blueprint needs (Azure handler, `GitHub` ConnectionInfo, appliance exec and egress): see [../BP-nibk4erf/README.md](../BP-nibk4erf/README.md).
- Because the stack is subscription-scoped, the handler service principal additionally needs, at subscription scope: `Microsoft.Resources/subscriptions/resourceGroups/write` and `/delete`, `Microsoft.Resources/deploymentStacks/*` and `Microsoft.Resources/deployments/*`. Contributor on the subscription covers this.

## Setup
1. Follow [../../docs/bicep-deployment-setup.md](../../docs/bicep-deployment-setup.md), in particular section 4a (template scopes) and "Optional: offer the new resource group on the Environment".
2. Re-enter the `GitHub` ConnectionInfo token after every repo sync. The quickstart repo is public but is still fetched through this connection.
3. The default Ref is `master`, a mutable branch. Pin a commit SHA in the deployment item's parameter defaults and in the form's hidden Ref field before using this beyond a demo.
4. The Location dropdown in FRM-i9zadhpc is a fixed list of regions; trim or extend it to match your Environments.

## Notes
- The job pauses after the what-if preview; approve with "Continue Job" (cb_admin only). No Resource Group field is offered because the template creates one.
- "Add Resource Group to Environment" links the new name as a `resource_group_arm` option on the provisioning Environment (stored on the resource as `bicep_env_id`) so it appears in VM order forms without a handler sync. Unticked, the step is a no-op; missing metadata yields WARNING, not FAILURE.
- Deleting the resource deletes the stack and the resource group it created (`actionOnUnmanage.resourceGroups = delete`). Azure refuses if unmanaged resources were since placed in that group; the teardown reports the 409 with guidance and never force-deletes.
- The teardown removes the resource group option from every Environment on the same Azure handler (subscription) plus the Environments recorded at build time. Only the link is removed; the shared CustomFieldValue is never deleted.
- Only the template's directory is fetched from the large quickstart repo (sparse fetch), so an order transfers kilobytes, not the full archive.
