# Sample module: Azure VM (no-code ready)

One Azure virtual machine (Linux or Windows, password login) with a NIC on an existing subnet. Written for both HCP Terraform blueprints in this repo: the shipped order forms of **HCP Terraform No-Code Module** (`BP-00meiwwz`) and **HCP Terraform VM** (`BP-b0qm83lh`) use exactly these variable names.

It follows HashiCorp's no-code module rules: standard module layout in the repository root, and the `azurerm` provider declared in the module itself with no `subscription_id`. The provider reads `ARM_SUBSCRIPTION_ID` / `ARM_TENANT_ID` from the workspace (CloudBolt writes them from the chosen Environment) and the client credentials from the project's variable set.

## Variables

| Variable | Form field | Notes |
|---|---|---|
| `vm_name` | VM Name | 1–64 chars, letters/numbers/hyphens. Windows computer name is the first 15. |
| `resource_group_name` | Resource Group | Existing; the VM and NIC go here. |
| `subnet_id` | Subnet | Full ARM ID. The VM is created in the subnet's VNet region. |
| `vm_size` | VM Size | e.g. `Standard_B2s`. |
| `admin_username` | Admin Username | Default `azureadmin`. |
| `admin_password` | Admin Password | Sensitive. The form writes it to HCP Terraform as a sensitive variable. |
| `os_image` | OS Image | `publisher:offer:sku:version`, the format the Form Options webhook submits. |
| `tags` | Tags | `map(string)`. |
| `os_type` | (none) | `auto` (default), `linux` or `windows`. `auto` picks Windows when the image publisher or offer contains "windows". |
| `os_disk_storage_account_type` | (none) | Default `Standard_LRS`. |

Outputs: `vm_name` (names the CloudBolt resource), `cloudbolt_vm_ids` (the VM's ARM resource ID in a list; CloudBolt creates one child Server record per entry), `vm_id`, `private_ip_address`, `location`, `os_type`. No secret outputs; add `sensitive = true` to any you introduce.

## Use with the No-Code Module blueprint

1. Copy this folder into its own Git repository (module files at the root) and push a semver tag, e.g. `v1.0.0`.
2. Publish it to the organization's private registry, enable no-code provisioning, pin the version and define variable options (at least `vm_size`, which the shipped form lists from HCP). Steps: [docs/hcp-no-code-setup.md](../../../hcp-no-code-setup.md) §2–§4.
3. Pin the `nocode-…` ID in the blueprint's form. The form's Module Variables panel already matches this module.

## Use with the HCP Terraform VM blueprint

Point the blueprint's `tfc_repo_identifier` / `tfc_branch` at the repository from step 1; no registry publishing needed. Steps: [docs/hcp-terraform-setup.md](../../../hcp-terraform-setup.md) §5 and §8.

## Try it outside CloudBolt

```bash
export ARM_CLIENT_ID=... ARM_CLIENT_SECRET=... ARM_TENANT_ID=... ARM_SUBSCRIPTION_ID=...
terraform init
terraform plan -var vm_name=sample-vm-01 -var resource_group_name=<rg> \
  -var subnet_id=/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<subnet> \
  -var vm_size=Standard_B2s -var admin_password='<password>' \
  -var os_image=Canonical:ubuntu-24_04-lts:server:latest
```
