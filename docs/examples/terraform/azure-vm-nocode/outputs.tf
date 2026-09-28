# CloudBolt records every non-sensitive output on the resource as a
# tfc_output_<name> custom field, in plaintext. Mark any output that carries a
# secret `sensitive = true`: HCP Terraform then returns it as null and CloudBolt
# never stores it. This module deliberately has no password output.

output "vm_name" {
  description = "Name of the VM. CloudBolt uses a vm_name (or name) output to name the resource."
  value       = var.vm_name
}

output "vm_id" {
  description = "Azure resource ID of the VM."
  value       = one(concat(azurerm_linux_virtual_machine.vm[*].id, azurerm_windows_virtual_machine.vm[*].id))
}

output "private_ip_address" {
  description = "Primary private IP address of the VM's NIC."
  value       = azurerm_network_interface.nic.private_ip_address
}

output "location" {
  description = "Azure region the VM was created in (the subnet's VNet region)."
  value       = data.azurerm_virtual_network.vnet.location
}

output "os_type" {
  description = "linux or windows, as resolved from os_type and the image."
  value       = local.is_windows ? "windows" : "linux"
}
