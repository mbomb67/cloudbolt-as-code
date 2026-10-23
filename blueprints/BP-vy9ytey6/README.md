# Linux VM with Workflow

Deploys a Linux VM, then runs a Runbooks workflow on it with the shared Run Workflow build item. It creates no resource: the VM is what CloudBolt tracks through its lifecycle. [Windows VM with Workflow](../BP-6ddlyguv/) is the Windows twin.

## Contents
| Role | ID | Name |
|---|---|---|
| VM item | BDI-8edh5z7o | VM (OS family Linux, or a family under it) |
| Build item | BDI-l252miz4 | Run Workflow (plugin OHK-69gk5ne9, the plug-in of the [Run Workflow](../../server_actions/SVA-j7rbsh39/) action) |
| Custom form | FRM-j7w530h7 | Linux VM with Workflow |
| Form function | FJS-hufo9zd4 | runbookWorkflowItems |

## Setup
Sync the Runbooks package (`packages/PKG-v20qv308`) and give the groups that should order this blueprint deploy permission. The package brings the example workflow [Prepare Linux app host](../../runbook_workflows/SWF-69r5mmdb/); upload its config file as its README says before ordering with it.

## Choosing the workflow
- **User's choice** (as shipped): the Workflow dropdown lists the enabled workflows whose OS family and OS build filters fit Linux VMs. Picking one shows a panel with just the inputs the workflow asks for. The form function `runbookWorkflowItems` loads that panel from the Runbook Workflows webhook and submits the answers as the item's Workflow inputs.
- **Pinned**: in the custom form, replace the Workflow dropdown with a hidden text question named `plugin-bdi-l252miz4.workflow` whose `defaultValue` is the workflow's name; the panel still asks for its inputs. Without a custom form, give the item's Workflow input a default in the Build tab instead (a custom form ignores item defaults).
- Without a custom form, the native order form shows Workflow inputs as JSON, pre-filled with the chosen workflow's prompts.
- A prompt that Workflow inputs leaves empty takes the value of a parameter with the same name on the VM (or on the resource, in a blueprint that creates one), so order-form or environment parameters can answer it.
- The workflow's filters and every step are checked against the new VM before the first step runs; a workflow that does not fit fails the order.

## Adding the item to another blueprint
Add a CloudBolt Plug-in build item after the VM item and choose the existing action **Run Workflow**. It runs on every active VM the order built, or on the resource's VMs in a blueprint that creates a resource (one child job per VM when there are several), and its outputs, each step's outputs by step key, are available to later build items.

To get the prompts panel on that blueprint's custom form, attach the form function `runbookWorkflowItems` to the form and add a hidden question `{"type": "text", "name": "runbook_workflow_items", "visible": false, "defaultValueExpression": "runbookWorkflowItems()"}`, with `clearInvisibleValues` set to `none`. The function handles every Run Workflow item on the form (any question named `plugin-bdi-<id>.workflow`), adding the hidden Workflow inputs question itself when the form lacks it.
