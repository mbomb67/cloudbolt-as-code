locals {
  # /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<subnet>
  # split("/") on a leading-slash path yields an empty first element, so the
  # resource group is index 4 and the VNet name index 8.
  subnet_parts        = split("/", var.subnet_id)
  vnet_resource_group = local.subnet_parts[4]
  vnet_name           = local.subnet_parts[8]

  image_parts = split(":", var.os_image)
  image = {
    publisher = local.image_parts[0]
    offer     = local.image_parts[1]
    sku       = local.image_parts[2]
    version   = local.image_parts[3]
  }

  is_windows = (
    var.os_type == "windows" ||
    (var.os_type == "auto" && can(regex("(?i)windows", "${local.image.publisher}:${local.image.offer}")))
  )
}

# The VM must live in the VNet's region; read it from the VNet rather than
# asking the orderer for a location.
# Docs: https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/data-sources/virtual_network
data "azurerm_virtual_network" "vnet" {
  name                = local.vnet_name
  resource_group_name = local.vnet_resource_group
}

# Docs: https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/network_interface
resource "azurerm_network_interface" "nic" {
  name                = "${var.vm_name}-nic"
  location            = data.azurerm_virtual_network.vnet.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  ip_configuration {
    name                          = "primary"
    subnet_id                     = var.subnet_id
    private_ip_address_allocation = "Dynamic"
  }
}

# Docs: https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/linux_virtual_machine
resource "azurerm_linux_virtual_machine" "vm" {
  count = local.is_windows ? 0 : 1

  name                  = var.vm_name
  resource_group_name   = var.resource_group_name
  location              = data.azurerm_virtual_network.vnet.location
  size                  = var.vm_size
  admin_username        = var.admin_username
  admin_password        = var.admin_password
  network_interface_ids = [azurerm_network_interface.nic.id]
  tags                  = var.tags

  # Password login; the provider defaults this to true (SSH keys only) and
  # requires false whenever admin_password is set.
  disable_password_authentication = false

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = var.os_disk_storage_account_type
  }

  source_image_reference {
    publisher = local.image.publisher
    offer     = local.image.offer
    sku       = local.image.sku
    version   = local.image.version
  }
}

# Docs: https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/windows_virtual_machine
resource "azurerm_windows_virtual_machine" "vm" {
  count = local.is_windows ? 1 : 0

  name                  = var.vm_name
  resource_group_name   = var.resource_group_name
  location              = data.azurerm_virtual_network.vnet.location
  size                  = var.vm_size
  admin_username        = var.admin_username
  admin_password        = var.admin_password
  network_interface_ids = [azurerm_network_interface.nic.id]
  tags                  = var.tags

  # Azure caps Windows computer names at 15 characters; the Azure resource
  # name (vm_name) may be longer.
  # Docs: https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules#microsoftcompute
  computer_name = substr(var.vm_name, 0, 15)

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = var.os_disk_storage_account_type
  }

  source_image_reference {
    publisher = local.image.publisher
    offer     = local.image.offer
    sku       = local.image.sku
    version   = local.image.version
  }
}
