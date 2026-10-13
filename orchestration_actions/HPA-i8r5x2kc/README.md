# ServiceNow Request: Close on Denial

At "Post-Order Denied", closes the order's RITM as Closed Incomplete with the CloudBolt approver and reason in the close notes. Denials that originated in ServiceNow (mirrored by the Approval Sync job) and RITMs ServiceNow already closed are skipped.

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-i8r5x2kc | ServiceNow Request: Close on Denial (Post-Order Denied, continue on failure) |
| Plugin | OHK-w8exctx7 | ServiceNow Request: Close on Denial |
| Shared module | SHM-ww6b1uwj | servicenow_requests |

## Setup
Read [../../docs/servicenow-request-setup.md](../../docs/servicenow-request-setup.md). Only the ConnectionInfo name is pinned.
