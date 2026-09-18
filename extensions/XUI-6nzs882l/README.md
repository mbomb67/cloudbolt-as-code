# Azure Patches (XUI)

Adds a **Patching** server tab to Azure VMs. The tab lists the VM's most recent Azure patch assessment (read via Azure Resource Graph) and provides **Scan for Patches** and **Apply all Patches** buttons, each of which runs as a CloudBolt job.

## Prerequisites
- CloudBolt 8.6 or later.
- Server must be on an Azure ARM resource handler and its Azure VM Agent must report `Ready`; the tab is hidden otherwise.
- The resource handler's service principal needs `Microsoft.ResourceGraph/resources/read` plus `Microsoft.Compute/virtualMachines/assessPatches/action` and `installPatches/action` on the VM.

## Setup
1. Sync the repository and confirm the extension is enabled under Admin > Extensions.
2. On first use, the extension registers an orchestration hook named `Azure VM Patching` whose module is `actions/scan_for_patches.py`; no manual hook creation is needed.

## Notes
- If the table is empty, run Scan for Patches first; the tab only shows stored assessment results.
- Apply all Patches installs every classification (Windows) or every package (Linux), reboots if required, and waits up to two hours.
- Azure endpoints are hard-coded to `management.azure.com` (public cloud only).

See [package readme](azure_patches/readme.md) for details.
