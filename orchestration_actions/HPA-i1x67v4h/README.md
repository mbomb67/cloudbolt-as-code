# ServiceNow Request: Open on Submit

Opens a ServiceNow Request (REQ) with one Requested Item (RITM) at "Order Submitted for Approval", so ServiceNow runs the approval while CloudBolt remains the catalog and the fulfiller. Orders one generic catalog item through the Service Catalog API when its sys_id is pinned; otherwise inserts `sc_request` and `sc_req_item` through the Table API. Enabling this action stops CloudBolt's approver emails; the Approval Sync job mirrors ServiceNow's decision onto the order.

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-i1x67v4h | ServiceNow Request: Open on Submit (Order Submitted for Approval) |
| Plugin | OHK-ehhju9cn | ServiceNow Request: Open on Submit |
| Shared module | SHM-ww6b1uwj | servicenow_requests |
| Companions | HPA-smbwmzj0 (OHK-nd8zo7zp), RJB-do33f7f3, HPA-i8r5x2kc, HPA-fxo5hujp | Open on Approval, Approval Sync, Close on Denial, Close on Completion |

## Setup
Read [../../docs/servicenow-request-setup.md](../../docs/servicenow-request-setup.md): ServiceNow catalog item and variable names, integration user permissions, ConnectionInfo, pinned inputs, and the order in which to enable the actions.

## Notes
- Requested for is the order recipient or owner, matched to `sys_user` by email then username. `servicenow_requested_for_miss` decides whether an unmatched owner fails the order or opens the request for the integration user.
- The RITM's `correlation_id` is the CloudBolt order global id; `short_description` and `description` are rewritten after ordering so approvers see the blueprint and target.
- The order item gets two URL parameters, ServiceNow Request and ServiceNow RITM, that open the records; nothing else about the request is stored in CloudBolt.
- Five seconds after the request is created the action reads the decision once and approves or denies the order if ServiceNow already decided; otherwise the Approval Sync job (every five minutes) picks it up.
- A failure here is shown to the user at submission and leaves the order pending.
