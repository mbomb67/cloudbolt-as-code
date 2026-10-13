# ServiceNow Request: Open on Approval

Runs the same plugin as Open on Submit at "Post-Order Approval". Orders that never reached ServiceNow because CloudBolt approved them first (group or environment auto-approval, owner is an approver, or a CloudBolt approver acted) still get a REQ/RITM, opened with `cloudbolt_approval=approved` so the catalog flow skips its approval step. Orders that already have a RITM are left alone.

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-smbwmzj0 | ServiceNow Request: Open on Approval (Post-Order Approval, continue on failure) |
| Plugin | OHK-o4qmnffy | ServiceNow Request: Open |
| Shared module | SHM-ww6b1uwj | servicenow_requests |

## Setup
Read [../../docs/servicenow-request-setup.md](../../docs/servicenow-request-setup.md). Pin the same inputs as HPA-i1x67v4h.

## Notes
- `continue_on_failure` is true: a ServiceNow outage does not fail an order CloudBolt has already approved; the failure is logged on the order.
