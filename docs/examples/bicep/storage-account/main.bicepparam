// Example parameter file. The scaffold-bicep skill offers these values as the
// proposed defaults during curation (file value beats template default, R22).
// It is a scaffold-time default source ONLY — the engine never reads a
// parameter file at deploy time; deploy-time values come from CloudBolt.
using './main.bicep'

param storageAccountName = 'cbteststorage001'
param sku = 'Standard_ZRS'
param accountKind = 'StorageV2'
param accessTier = 'Hot'
param tags = {
  managedBy: 'CloudBolt'
  purpose: 'bicep-engine-test'
}
