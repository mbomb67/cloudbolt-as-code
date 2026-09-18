// Generic test template for the CloudBolt Bicep deployment engine.
//
// Deploys a single Azure Storage Account. It is intentionally shaped to
// exercise every branch of the engine's parameter-schema extractor and the
// scaffold-bicep curation flow:
//   - required input, no default          -> storageAccountName
//   - length constraints                  -> storageAccountName (@minLength/@maxLength)
//   - expression default (pin-by-omission)-> location (resourceGroup().location)
//   - literal default + @allowed dropdown -> sku, accountKind, accessTier
//   - untyped object -> JSON free-text    -> tags
//   - output-driven resource naming       -> output `resourceName`
//
// Azure resource reference (AGENTS.md cardinal rule 5 — cited, not guessed):
// https://learn.microsoft.com/en-us/azure/templates/microsoft.storage/storageaccounts
// api-version 2026-04-01 (current stable, verified June 2026).

@description('Globally-unique storage account name: 3-24 lowercase letters and digits.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Azure region. Defaults to the resource group location, evaluated by Azure at deploy time (the engine omits this when pinned).')
param location string = resourceGroup().location

@description('Replication / SKU.')
@allowed([
  'Standard_LRS'
  'Standard_GRS'
  'Standard_ZRS'
  'Standard_RAGRS'
  'Premium_LRS'
])
param sku string = 'Standard_LRS'

@description('Storage account kind (both supported kinds accept an access tier).')
@allowed([
  'StorageV2'
  'BlobStorage'
])
param accountKind string = 'StorageV2'

@description('Blob access tier.')
@allowed([
  'Hot'
  'Cool'
])
param accessTier string = 'Hot'

@description('Optional resource tags as a JSON object, e.g. {"env":"dev","owner":"team"}.')
param tags object = {}

resource storageAccount 'Microsoft.Storage/storageAccounts@2026-04-01' = {
  name: storageAccountName
  location: location
  kind: accountKind
  sku: {
    name: sku
  }
  tags: tags
  properties: {
    accessTier: accessTier
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

// The engine sets the CloudBolt resource name from an output named
// `resourceName` (see RESOURCE_NAME_OUTPUT_CANDIDATES in the bicep_engine module).
@description('Provisioned storage account name; drives the CloudBolt resource name.')
output resourceName string = storageAccount.name

@description('Full ARM resource ID of the storage account.')
output storageAccountId string = storageAccount.id

@description('Primary blob service endpoint.')
output primaryBlobEndpoint string = storageAccount.properties.primaryEndpoints.blob
