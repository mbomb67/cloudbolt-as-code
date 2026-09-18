# Azure NSG - Attach VM to NSG

Post-Provision orchestration action that attaches each newly provisioned Azure VM's network interface(s) to the Network Security Group the requester chose on the order form (the `azure_nsg` parameter). Servers without an `azure_nsg` value are skipped. Ships disabled.

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-vo2ghe4x | Azure NSG - Attach VM to NSG (Post-Provision, run_seq 1, disabled) |
| Plugin | OHK-t8fjx0kz | Azure NSG - Attach VM to NSG |
| Plugin | OHK-vswf8b1m | Azure NSG - Generate Options (populates `azure_nsg`) |

## Prerequisites
- Azure (ARM) resource handler; the VMs must be provisioned by CloudBolt (the Azure VM name is taken to equal the server hostname and the resource group from the server's Azure info).
- The handler's service principal needs `Microsoft.Compute/virtualMachines/read`, read/write on `Microsoft.Network/networkInterfaces` (Network Contributor is sufficient) and `Microsoft.Network/networkSecurityGroups/read` to list NSGs on the order form.
- Custom fields `azure_nsg` and `azure_nsg_applied_id` (STR, shown on servers) are created automatically on first run, but `azure_nsg` must be present on the order form for users to pick an NSG.

## Setup
1. Sync the repository; the action imports disabled together with both plugins.
2. Open the `azure_nsg` parameter (Admin > Parameters; create it as type STR if it does not exist), set Options to Generated and select "Azure NSG - Generate Options" (OHK-vswf8b1m). Its option values are NSG Resource IDs, which is what this action expects.
3. Add `azure_nsg` to the Azure environments or blueprints where users should be able to choose an NSG. Options are scoped to the selected Environment's subscription and region, with a "None (do not attach an NSG)" placeholder first.
4. Enable the action (Admin > Orchestration Actions > Post-Provision).

## Notes
- The NSG is attached per NIC (the NIC's `networkSecurityGroup`), not on the subnet. A NIC already attached to the same NSG is reported as "already attached"; a different NSG on the NIC is replaced.
- Every server on the job is processed; one server's failure is recorded and the others still run, and any failure makes the job FAILURE. If no server has `azure_nsg` set the action returns SUCCESS.
- On success the NSG Resource ID is written to `azure_nsg_applied_id` on the server.
- An NSG can only be attached to a NIC in its own region; the options plugin filters to the Environment's region (`node_location`).
