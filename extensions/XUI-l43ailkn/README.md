# Ssm Inventory (XUI)

Adds two server tabs for AWS EC2 instances managed by AWS Systems Manager: **Inventory** lists installed applications (Name, Version, Release) from SSM Inventory, and **Patching** lists patch state from SSM Patch Manager with a **Patch EC2 Instance** dialog that runs `AWS-RunPatchBaseline` (Install or Scan, reboot if needed or no reboot) as a CloudBolt job.

## Prerequisites
- CloudBolt 8.6 or later.
- Server must be on an AWS resource handler and reported as a managed instance by SSM (SSM Agent running, instance profile with `AmazonSSMManagedInstanceCore`); tabs are hidden otherwise.
- SSM Inventory collection enabled for the instance so `AWS:Application` data exists.
- The resource handler's access key needs: `ssm:DescribeInstanceInformation`, `ssm:ListInventoryEntries`, `ssm:DescribeInstancePatches`, `ssm:SendCommand`, `ssm:GetCommandInvocation`.

## Setup
1. Sync the repository and confirm the extension is enabled under Admin > Extensions.
2. On first use of the Patch dialog, the extension registers an orchestration hook named `SSM Patch EC2 Server Hook` whose module is `patch_hook.py`; no manual hook creation is needed. `patch_hook.py` is the job entry point and calls `patch_ec2_instance` in `views.py`.

## Notes
- The patch job polls the SSM command every 10 seconds and fails the job if the command ends in any state other than `Success` (command timeout 3600 seconds).
- Tab visibility calls `ssm:DescribeInstanceInformation` on every server page load.
