# ServiceNow Request: Open on Approval

Runs the same plugin as Open on Submit at "Post-Order Approval", the last hook before an approved order's jobs are created. Super admins, approvers ordering for themselves, and auto-approving groups never reach the submit hook, so this is where ServiceNow gets the last word.

With `servicenow_wait_for_approval` = `true` (default): if the order is subject to ServiceNow (it has a RITM, or Open on Submit is enabled and in scope for its group and blueprint), a missing RITM is opened as pending and the decision is read; approved orders proceed, rejected orders are denied, anything else is set back to Pending until the Approval Sync job sees the approval. Orders outside Open on Submit's scope are ignored. With the pin `false`, CloudBolt-approved orders proceed and a missing RITM is opened flagged approved as a record only.

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-smbwmzj0 | ServiceNow Request: Open on Approval (Post-Order Approval, continue on failure) |
| Plugin | OHK-o4qmnffy | ServiceNow Request: Open |
| Shared module | SHM-ww6b1uwj | servicenow_requests |

## Setup
Read [../../docs/servicenow-request-setup.md](../../docs/servicenow-request-setup.md). Pin the same inputs as HPA-i1x67v4h.

## Notes
- `continue_on_failure` is true. With the wait pin on, `servicenow_unavailable_policy` decides what an unreachable ServiceNow means: `hold` (default) sets the order back to Pending with a history line, and approving it again in CloudBolt retries; `proceed` lets it run.
- The sync job must not approve as a user who already approved in CloudBolt, or `approve()` refuses; leave `cloudbolt_approver_username` empty or set it to a service user.
- When an auto-approved order is held, CloudBolt's submission banner may say the order is paused by Flow Control; the order shows Pending and its history explains the hold.
