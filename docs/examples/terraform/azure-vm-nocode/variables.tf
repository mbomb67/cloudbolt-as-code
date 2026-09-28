# Variable names are the CloudBolt order form's field names. Keep them stable;
# renaming one means editing the form.

variable "vm_name" {
  description = "Name of the virtual machine. Also the CloudBolt resource name after apply."
  type        = string

  validation {
    # Same rule the CloudBolt form and build plugin enforce. Windows computer
    # names are further limited to 15 characters (see main.tf).
    # Docs: https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules#microsoftcompute
    condition     = can(regex("^[a-zA-Z0-9]([a-zA-Z0-9-]{0,62}[a-zA-Z0-9])?$", var.vm_name))
    error_message = "vm_name must be 1-64 characters: letters, numbers and hyphens, starting and ending with a letter or number."
  }
}

variable "resource_group_name" {
  description = "Existing resource group the VM and its NIC are created in."
  type        = string
}

variable "subnet_id" {
  description = "Full ARM resource ID of the existing subnet to attach the NIC to. The VM is created in the subnet's VNet region."
  type        = string

  validation {
    condition     = can(regex("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\\.Network/virtualNetworks/[^/]+/subnets/[^/]+$", var.subnet_id))
    error_message = "subnet_id must be a full subnet resource ID: /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<subnet>."
  }
}

variable "vm_size" {
  description = "Azure VM size (SKU), e.g. Standard_B2s."
  type        = string
}

variable "admin_username" {
  description = "Local administrator username."
  type        = string
  default     = "azureadmin"
}

variable "admin_password" {
  description = "Local administrator password. Must meet Azure's complexity rules. Written to HCP Terraform as a sensitive variable by the CloudBolt form."
  type        = string
  sensitive   = true
}

variable "os_image" {
  description = "Marketplace image as publisher:offer:sku:version, e.g. Canonical:ubuntu-24_04-lts:server:latest. This is the format CloudBolt's Form Options webhook submits."
  type        = string

  validation {
    condition     = length(split(":", var.os_image)) == 4
    error_message = "os_image must be publisher:offer:sku:version."
  }
}

variable "os_type" {
  description = "linux, windows, or auto. auto picks windows when the image publisher or offer contains 'windows', linux otherwise."
  type        = string
  default     = "auto"

  validation {
    condition     = contains(["auto", "linux", "windows"], var.os_type)
    error_message = "os_type must be auto, linux or windows."
  }
}

variable "os_disk_storage_account_type" {
  description = "Storage account type of the OS disk."
  type        = string
  default     = "Standard_LRS"
}

variable "tags" {
  description = "Tags applied to every resource this module creates."
  type        = map(string)
  default     = {}
}
