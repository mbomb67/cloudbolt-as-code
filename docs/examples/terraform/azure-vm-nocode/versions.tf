terraform {
  required_version = ">= 1.5"

  required_providers {
    # Docs: https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.0, < 6.0"
    }
  }
}

# A no-code ready module is the root of its workspace, so it carries the
# provider block itself (a normal child module leaves this to its caller).
#
# No subscription_id or tenant_id here, on purpose: the azurerm provider reads
# ARM_SUBSCRIPTION_ID / ARM_TENANT_ID from the workspace's environment
# variables, which CloudBolt writes per deployment from the chosen Environment,
# and ARM_CLIENT_ID / ARM_CLIENT_SECRET from the project's variable set.
provider "azurerm" {
  features {}
}
