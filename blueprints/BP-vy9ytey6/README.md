# Server with Workflow

Deploys a Windows Server or Oracle Linux VM, then runs a server workflow on it with the shared Run Workflow build item.

## Contents
| Role | ID | Name |
|---|---|---|
| Server tier | BDI-8edh5z7o | Server |
| Build item | BDI-l252miz4 | Run Workflow (plugin OHK-69gk5ne9, the plug-in of the [Run Workflow](../../server_actions/SVA-j7rbsh39/) server action) |
| Custom form | FRM-j7w530h7 | Server with Workflow |
| Form function | FJS-hufo9zd4 | serverWorkflowItems |

## Setup
Sync the Server Workflows package (`packages/PKG-vfnycv8s`), build a workflow on Admin > Custom Extensions > Server Workflows, and give the groups that should order this blueprint deploy permission.

## Choosing the workflow
- **User's choice** (as shipped): the custom form lists every enabled workflow, and picking one shows a panel with just the inputs the workflow asks for. The form function `serverWorkflowItems` loads that panel from the Server Workflows webhook and submits the answers as the item's Workflow inputs.
- **Pinned**: in the custom form, replace the Workflow dropdown with a hidden text question named `plugin-bdi-l252miz4.workflow` whose `defaultValue` is the workflow's name; the panel still asks for its inputs. Without a custom form, give the item's Workflow input a default in the Build tab instead (a custom form ignores item defaults).
- Without a custom form, the native order form shows Workflow inputs as JSON, pre-filled with the chosen workflow's prompts.
- A prompt that Workflow inputs leaves empty takes the value of a parameter with the same name on the resource, else on the server, so blueprint parameters or environment parameters can answer it.
- The workflow's filters and every step are checked against the new server before the first step runs; a workflow that does not fit fails the order.

## Adding the item to another blueprint
Add a CloudBolt Plug-in build item after the server tier and choose the existing action **Run Workflow**. It runs on every active server of the order's resource (one child job per server when there are several), and its outputs, each step's outputs by step key, are available to later build items.

To get the prompts panel on that blueprint's custom form, attach the form function `serverWorkflowItems` to the form and add a hidden question `{"type": "text", "name": "server_workflow_items", "visible": false, "defaultValueExpression": "serverWorkflowItems()"}`, with `clearInvisibleValues` set to `none`. The function handles every Run Workflow item on the form (any question named `plugin-bdi-<id>.workflow`), adding the hidden Workflow inputs question itself when the form lacks it.
