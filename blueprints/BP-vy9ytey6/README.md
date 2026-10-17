# Server with Workflow

Deploys a Windows Server or Oracle Linux VM, then runs a server workflow on it with the shared Run Workflow build item.

## Contents
| Role | ID | Name |
|---|---|---|
| Server tier | BDI-8edh5z7o | Server |
| Build item | BDI-l252miz4 | Run Workflow (plugin OHK-69gk5ne9, the plug-in of the [Run Workflow](../../server_actions/SVA-j7rbsh39/) server action) |

## Setup
Sync the Server Automation package (`packages/PKG-v20qv308`), build a workflow on Admin > Custom Extensions > Server Workflows, and give the groups that should order this blueprint deploy permission.

## Choosing the workflow
- **User's choice** (as shipped): the order form lists every enabled workflow. Picking one pre-fills Workflow inputs with what it asks for, as JSON.
- **Pinned**: in the blueprint's Build tab, give the Run Workflow item's Workflow input a default, the workflow's name. The field is then hidden on the order form.
- A prompt that Workflow inputs leaves empty takes the value of a parameter with the same name on the resource, else on the server, so blueprint parameters or environment parameters can answer it.
- The workflow's filters and every step are checked against the new server before the first step runs; a workflow that does not fit fails the order.

## Adding the item to another blueprint
Add a CloudBolt Plug-in build item after the server tier and choose the existing action **Run Workflow**. It runs on every active server of the order's resource (one child job per server when there are several), and its outputs, each step's outputs by step key, are available to later build items.
