# Azure PS (Deployment Script) - List Resource Groups
#
# Lists the resource groups in the connected subscription whose names match
# the Name Filter input (wildcards at the start and/or end, e.g. rg-prod-*),
# with location, provisioning state and tag count.
#
# Runs through the "Run an Azure PowerShell Script (Deployment Script)"
# blueprint, which signs the Az PowerShell session in before this script
# starts, in a container Azure runs for the order (PowerShell 7 on Linux,
# Az modules preinstalled). Do not call
# Connect-AzAccount here; the context is already set for the selected
# environment's subscription. CloudBolt substitutes the action inputs below
# before the script is sent to Azure as a deployment script.
#
# Error handling: CloudBolt sees only what the script prints plus its exit
# code, so failures are written with Write-Output and the script exits 1.
#
# Get-AzResourceGroup: https://learn.microsoft.com/en-us/powershell/module/az.resources/get-azresourcegroup
#   -Name supports wildcards at the beginning and/or the end of the string.

$ErrorActionPreference = 'Stop'

$nameFilter = '{{ name_filter }}'.Trim()

try {
    $context = Get-AzContext
    if (-not $context) { throw "No Azure context is available; the sign-in step did not run." }
    Write-Output ("Subscription: {0} ({1})" -f $context.Subscription.Name, $context.Subscription.Id)

    if ([string]::IsNullOrWhiteSpace($nameFilter) -or $nameFilter -eq '*') {
        $groups = @(Get-AzResourceGroup)
        Write-Output ("Resource groups: {0}" -f $groups.Count)
    } else {
        $groups = @(Get-AzResourceGroup -Name $nameFilter)
        Write-Output ("Resource groups matching '{0}': {1}" -f $nameFilter, $groups.Count)
    }

    $groups |
        Sort-Object ResourceGroupName |
        Select-Object ResourceGroupName, Location, ProvisioningState,
            @{ Name = 'TagCount'; Expression = { if ($_.Tags) { $_.Tags.Count } else { 0 } } } |
        Format-Table -AutoSize |
        Out-String -Width 200 |
        Write-Output
} catch {
    Write-Output ("ERROR: " + $_.Exception.GetType().Name + ": " + $_.Exception.Message)
    if ($_.InvocationInfo -and $_.InvocationInfo.PositionMessage) { Write-Output $_.InvocationInfo.PositionMessage }
    exit 1
}
