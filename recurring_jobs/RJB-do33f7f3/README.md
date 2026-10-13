# ServiceNow Request Approval Sync

Recurring job that reads one field on each pending order's RITM (or REQ) and approves or denies the CloudBolt order to match. Orders without a ServiceNow request are ignored, so orders that predate the integration are untouched. Orders cancelled in CloudBolt during the last seven days get their still-open RITM closed as incomplete.

## Contents
| Role | ID | Name |
|---|---|---|
| Recurring job | RJB-do33f7f3 | ServiceNow Request Approval Sync (schedule `*/2 * * * *`) |
| Plugin | OHK-bsqictlo | ServiceNow Request Approval Sync |
| Shared module | SHM-ww6b1uwj | servicenow_requests |
| Companions | HPA-i1x67v4h, HPA-smbwmzj0, HPA-i8r5x2kc, HPA-fxo5hujp | Open on Submit, Open on Approval, Close on Denial, Close on Completion |

## Setup
Read [../../docs/servicenow-request-setup.md](../../docs/servicenow-request-setup.md). Pins: ConnectionInfo name; `servicenow_approval_table` (`sc_req_item` or `sc_request`); `servicenow_approval_field` (`approval` by default; `stage` or `state` with matching values); approved and denied value lists; optional CloudBolt approver username recorded on the order.

## Notes
- A RITM in any closed state (3, 4, 7) counts as a denial even if `approval` never changed.
- A ServiceNow approval that CloudBolt refuses (quota exceeded, for example) is logged and retried on the next run; the order stays pending.
- `allow_parallel_jobs` is false so overlapping runs cannot approve an order twice.
