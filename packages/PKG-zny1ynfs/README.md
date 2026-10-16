# Data Disks from a Disk Map

Adds order-time data disks to Windows Server and Oracle Linux VMs through an environment disk_map parameter, a form function that turns it into a disk table, and Post-Provision orchestration actions that attach the disks and prepare them inside the guest.

Content package `PKG-zny1ynfs`, version 1.0.0. Sync it from the Content Packages admin page (Content Package Manager UI extension).

| Type | Name | Folder |
|---|---|---|
| Blueprint | Server with Data Disks | [blueprints/BP-g9zwqjoa](../../blueprints/BP-g9zwqjoa/) |
| Orchestration action | Attach Data Disks from Disk Map | [orchestration_actions/HPA-n67skws4](../../orchestration_actions/HPA-n67skws4/) |
| Orchestration action | Configure Data Disks from Disk Map (Oracle Linux) | [orchestration_actions/HPA-4ptcwgg4](../../orchestration_actions/HPA-4ptcwgg4/) |
| Orchestration action | Configure Data Disks from Disk Map (Windows) | [orchestration_actions/HPA-42uy9dyo](../../orchestration_actions/HPA-42uy9dyo/) |
| Parameter | Data Disks | [parameters/CF-q3bexgap](../../parameters/CF-q3bexgap/) |
