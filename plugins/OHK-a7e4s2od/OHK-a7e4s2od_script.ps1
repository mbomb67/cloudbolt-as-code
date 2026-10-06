# Azure PS - Tag Resource Group
#
# Merges one tag (Tag Name = Tag Value) onto the named resource group,
# keeping its other tags, then prints the resulting tag set.
#
# Runs through the "Run an Azure PowerShell Script" blueprint, which signs the
# Az PowerShell session in before this script starts. Do not call
# Connect-AzAccount here; the context is already set for the selected
# environment's subscription. CloudBolt substitutes the action inputs below
# before the script is sent to the Run on Server host.
#
# Error handling: CloudBolt sees only what the script prints plus its exit
# code, so failures are written with Write-Output and the script exits 1.
#
# Get-AzResourceGroup: https://learn.microsoft.com/en-us/powershell/module/az.resources/get-azresourcegroup
# Update-AzTag:        https://learn.microsoft.com/en-us/powershell/module/az.resources/update-aztag
#   -Operation Merge adds tags with new names and updates the values of existing ones.

$ErrorActionPreference = 'Stop'

$resourceGroupName = '{{ resource_group_name }}'.Trim()
$tagName  = '{{ tag_name }}'.Trim()
$tagValue = '{{ tag_value }}'

try {
    if (-not $resourceGroupName) { throw "Resource Group Name is required." }
    if (-not $tagName)           { throw "Tag Name is required." }

    $found = @(Get-AzResourceGroup -Name $resourceGroupName)
    if ($found.Count -ne 1) {
        throw ("Expected exactly one resource group named '{0}', found {1}." -f $resourceGroupName, $found.Count)
    }
    $rg = $found[0]

    Write-Output ("Tagging resource group {0} ({1}) with {2} = {3}" -f $rg.ResourceGroupName, $rg.Location, $tagName, $tagValue)
    Update-AzTag -ResourceId $rg.ResourceId -Tag @{ $tagName = $tagValue } -Operation Merge | Out-Null

    $tags = (Get-AzResourceGroup -Name $rg.ResourceGroupName).Tags
    Write-Output ("Tags now on {0}:" -f $rg.ResourceGroupName)
    if ($tags) {
        $tags.GetEnumerator() | Sort-Object Key | ForEach-Object { Write-Output ("  {0} = {1}" -f $_.Key, $_.Value) }
    } else {
        Write-Output "  (none)"
    }
} catch {
    Write-Output ("ERROR: " + $_.Exception.GetType().Name + ": " + $_.Exception.Message)
    if ($_.InvocationInfo -and $_.InvocationInfo.PositionMessage) { Write-Output $_.InvocationInfo.PositionMessage }
    exit 1
}
