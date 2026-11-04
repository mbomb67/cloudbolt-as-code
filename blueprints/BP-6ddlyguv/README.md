# Windows VM with Workflow

Deploys a Windows VM, then runs a Runbooks workflow on it with the shared Run Workflow build item. It creates no resource: the VM is what CloudBolt tracks through its lifecycle. It is the Windows twin of [Linux VM with Workflow](../BP-vy9ytey6/), whose README explains how the workflow is chosen, pinned and answered.

## Contents
| Role | ID | Name |
|---|---|---|
| VM item | BDI-d54scsxz | VM (OS family Windows, or a family under it) |
| Build item | BDI-5d8q0fy6 | Run Workflow (plugin OHK-69gk5ne9, the plug-in of the [Run Workflow](../../server_actions/SVA-j7rbsh39/) action) |
| Custom form | FRM-s6h73usp | Windows VM with Workflow |
| Form function | FJS-hufo9zd4 | runbookWorkflowItems |

## Setup
Sync the Runbooks package (`packages/PKG-v20qv308`) and give the groups that should order this blueprint deploy permission. The package brings the example workflow [Prepare Windows app host](../../runbook_workflows/SWF-7s01lyyk/), and its config file, which the sync stores in File Archives.

The Workflow dropdown lists the enabled workflows whose OS family and OS build filters fit Windows VMs. To pin one instead, replace the dropdown in the custom form with a hidden text question named `plugin-bdi-5d8q0fy6.workflow` whose `defaultValue` is the workflow's name.
