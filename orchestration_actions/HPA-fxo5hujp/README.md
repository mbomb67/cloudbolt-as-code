# ServiceNow Request: Close on Completion

At "Post-Order Execution" (every job of the order finished), closes the order's RITM: Closed Complete for SUCCESS and WARNING, Closed Incomplete for FAILURE. Each server the order produced is looked up in the CMDB and added to the RITM's Affected CIs (`task_ci`); the first becomes the RITM's Configuration item. A work note links back to the order, resources and servers. The ServiceNow identifiers are then written onto the created servers and resources.

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-fxo5hujp | ServiceNow Request: Close on Completion (Post-Order Execution, continue on failure) |
| Plugin | OHK-htrxkps2 | ServiceNow Request: Close |
| Shared module | SHM-ww6b1uwj | servicenow_requests |

## Prerequisites
- CloudBolt's CMDB integration creates the server CI at Post-Provision, storing the resource handler's VM id in `serial_number` and `asset_tag`. The lookup uses those, then the hostname.
- Resources that are not servers are attached only when the custom field `servicenow_ci_sys_id` is set on the resource.

## Setup
Read [../../docs/servicenow-request-setup.md](../../docs/servicenow-request-setup.md). Pins: ConnectionInfo name, CMDB table (`cmdb_ci_server`), optional CloudBolt base URL for the links.
