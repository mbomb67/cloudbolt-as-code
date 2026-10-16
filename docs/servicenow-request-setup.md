# ServiceNow Requests (REQ / RITM) for CloudBolt orders

CloudBolt stays the service catalog. Every order still produces a ServiceNow Request (REQ) with one Requested Item (RITM), so ServiceNow carries the approval and the audit trail while CloudBolt fulfills the order and closes the RITM with the resulting CIs attached. One generic ServiceNow catalog item serves every CloudBolt blueprint and action; nothing in ServiceNow changes when CloudBolt adds content.

## Lifecycle

| CloudBolt event | Content | ServiceNow effect |
|---|---|---|
| Order submitted (pending) | HPA-i1x67v4h "Open on Submit" | REQ + RITM opened, `cloudbolt_approval=pending`; the item's flow requests approval. The decision is read once right away, without waiting, so a request ServiceNow approved while it was being opened proceeds without waiting for the poll |
| Order approved in CloudBolt (auto-approval, super admin, approver click, or the sync job) | HPA-smbwmzj0 "Open on Approval" | With the wait pin on: RITM opened as pending if missing, decision read, order proceeds, is denied, or goes back to Pending. With it off: record-only RITM opened with `cloudbolt_approval=approved` |
| Every five minutes | RJB-do33f7f3 "Approval Sync" | Approval read on the RITM for catalog-path orders and on the REQ for Table API orders; CloudBolt order approved or denied to match; RITMs of orders cancelled in CloudBolt closed |
| Order denied in CloudBolt | HPA-i8r5x2kc "Close on Denial" | RITM closed incomplete with the reason |
| All jobs finished | HPA-fxo5hujp "Close on Completion" | RITM closed complete (or incomplete on failure), server CIs added as Affected CIs, first CI set as `cmdb_ci` and `configuration_item`, work note with links |

Mapping: CloudBolt order = REQ, order item = RITM, each server or resource = a row in the RITM's multi-row variable set before build and an Affected CI after. Day-2 actions, modifications and teardown open their own RITM against the same catalog item with `cloudbolt_action` set to `day2`, `modify` or `teardown`, but only when they create an order: a resource or server action runs as a plain job unless it is marked *Requires Approval*, and a plain job never reaches the order hook points. Mark the actions that must leave a ServiceNow record as requiring approval; the Delete action on a resource is one of them.

The blueprint order item (builds) or the targeted server or resource (day-2, modify, decommission) gets two URL parameters, **ServiceNow Request** and **ServiceNow RITM**, whose text is the record number and whose link opens the record (the record's form URL by number, `/sc_req_item.do?sysparm_query=number=RITM0010465`). They are copied onto the created servers and resources at completion. Nothing else is stored in CloudBolt: each action looks the RITM up by number and accepts it only when its `correlation_id` equals the order's global id. Each step also adds a line to the order's history.

Where approvals live follows ServiceNow's conventions. A catalog item's flow asks for approval on the RITM, one approval per item; the REQ is the cart and reflects the sum of its items. The out-of-the-box Service Catalog Request workflow adds a cart-level approval on the REQ only when the price crosses its threshold. The sync job therefore reads the RITM for catalog-path orders and the REQ for Table API orders, where no item flow runs; pin `servicenow_approval_table` to force one table.

## ServiceNow as the approval authority

Super admins, users who are approvers in their own group, and groups or environments with auto-approval never reach "Order Submitted for Approval"; CloudBolt approves their orders on the spot and only then runs "Post-Order Approval". The Open on Approval action therefore carries the policy pin `servicenow_wait_for_approval`:

- `true` (default): any order subject to ServiceNow waits for ServiceNow, whoever ordered it or clicked Approve in CloudBolt. The action opens the RITM if the submit hook never ran, reads the decision, and lets the order proceed only when ServiceNow approved; a rejection denies the order; anything else sets the order back to Pending until the sync job mirrors the approval. CloudBolt's own approval becomes a first gate.
- `false`: CloudBolt's approval stands; a missing RITM is opened flagged approved so ServiceNow records what is being built.

"Subject to ServiceNow" means the order already has a RITM, or Open on Submit is enabled and its group and blueprint constraints match the order. Disable ServiceNow for a blueprint or group by constraining or disabling Open on Submit; Open on Approval follows automatically.

`servicenow_unavailable_policy` (`hold` by default) decides what happens when ServiceNow cannot be reached while waiting: the order goes back to Pending with a history line and approving it again retries, or `proceed` lets it run.

## ServiceNow side

### 1. Integration user
- A local user with the `itil` and `catalog` roles (or your instance's equivalent for creating requests, reading `sys_user` and the CMDB, and writing `task_ci`).
- Allow it to request on behalf of others: system properties `glide.sc.req_for.roles` (add a role the user has) and `glide.sc.req_for.roles.default`. Without this, `requested_for` falls back to the integration user; the action then PATCHes `requested_for` through the Table API, which needs write access to that column.
- If `glide.sc.checkout.twostep` is true, `order_now` only fills a cart; the action submits the cart automatically.

### 2. Generic catalog item (catalog path, recommended)
Fastest: import the update set in [examples/servicenow/](examples/servicenow/README.md). It creates the item, both variable sets and their variables with the sys_id the Open actions carry by default (`dc46b35f3b378f50421a9bdc73e45a3b`), plus a generic flow that approves every RITM, so nothing needs pinning. Orders proceed as soon as CloudBolt sees the approval: at submission when the flow has already run, otherwise at the next Approval Sync run. Afterwards set the item's Category, then extend the flow with your approval policy as described in step 5. The rest of this step describes what the update set contains, for building it by hand or checking it.

Create one catalog item, for example "CloudBolt Automation Request", and note its sys_id. Add a variable set with these variables. Names must match exactly.

| Variable name | Type | Content |
|---|---|---|
| `cloudbolt_order_id` | Single-line text | CloudBolt order id |
| `cloudbolt_order_url` | URL or single-line text | Link to the order |
| `cloudbolt_action` | Single-line text | `build`, `day2`, `modify`, `teardown` or `other` |
| `cloudbolt_blueprint` | Single-line text | Blueprint name, or the action label |
| `cloudbolt_resource_type` | Single-line text | CloudBolt resource type label |
| `cloudbolt_group` | Single-line text | Ordering group |
| `cloudbolt_environment` | Single-line text | Environment name(s) |
| `cloudbolt_cloud` | Single-line text | Resource technology, e.g. Azure, AWS, VMware |
| `cloudbolt_estimated_cost` | Single-line text | Rate shown on the CloudBolt order |
| `cloudbolt_requested_for` | Single-line text | Requester display name and email |
| `cloudbolt_justification` | Multi-line text | Business justification field from the order, if any |
| `cloudbolt_approval` | Single-line text | `pending` or `approved` (already approved in CloudBolt) |
| `cloudbolt_items` | Multi-row variable set | One row per requested item; columns below |
| `cloudbolt_payload` | Multi-line text | Full order payload as JSON for machine use |

Multi-row variable set `cloudbolt_items` columns, all single-line text: `item_type`, `name`, `environment`, `size`, `quantity`, `details`.

Attach a Flow Designer flow to the item. The update set ships one that only sets the RITM's Approval to Approved (Service Catalog trigger, Update Record); insert your policy in front of that step. A full flow looks like this:
- If `cloudbolt_approval` is `approved`, skip approval and leave the RITM open; CloudBolt closes it.
- Otherwise request approval. Route on `cloudbolt_group`, `cloudbolt_environment`, `cloudbolt_cloud` or `cloudbolt_estimated_cost`; a lookup table owned by the ServiceNow team keeps routing a data change rather than a catalog change. If the flow records its decision on the RITM rather than the REQ, pin `servicenow_approval_table` to `sc_req_item` on the sync job.
- Do not generate catalog tasks or close the RITM in the flow. CloudBolt is the fulfiller and closes the RITM itself.
- An item with no flow attached never asks anyone, so orders wait forever. If you cannot build a flow, pin `servicenow_table_path_approvers` on the Open actions instead (user names, emails, or `manager`); the action then creates the approval records on the RITM itself. Do not do both, or approvals are requested twice.
- Optional: on rejection set the RITM state to Closed Incomplete so the RITM list stays tidy; the sync job treats any closed RITM as a denial.

### 3. Table path (no catalog item)
Leave `servicenow_catalog_item_sys_id` empty. The action inserts `sc_request` and `sc_req_item` directly with `approval` set to `requested` (or `approved` for orders already approved in CloudBolt). The catalog item flow does not run, but the out-of-the-box Service Catalog Request workflow does run on the `sc_request` insert and records its decision in the REQ's `approval` field (approving outright when no approval rule applies, for example a zero-cost request). To get a real RITM approval on this path, pin `servicenow_table_path_approvers` on the Open actions: comma-separated ServiceNow user names or emails, or `manager` for the requester's manager. The action then inserts one `sysapproval_approver` row per approver on the RITM, which is exactly what a flow would do; the approvers see it under **My Approvals** (or the RITM's Approvers related list) and click Approve or Reject there, never by editing the RITM's Approval field. The sync job reads those rows (any rejection denies; all approved approves). Without pinned approvers it reads the REQ's `approval` field, the only thing that moves on this path.

## CloudBolt side

1. Point the actions at ServiceNow. If Admin > ITSM already has a ServiceNow integration, nothing else is needed: the actions use its host and service account when no ConnectionInfo matches the pinned name. Otherwise create a ConnectionInfo named `ServiceNow` (Admin > Connection Info): protocol `https`, IP/Hostname `<instance>.service-now.com`, port 443, username and password of the integration user. Re-enter the password after every repo sync; exports redact it.
2. Sync this repo, then restart CloudBolt once so the `servicenow_requests` shared module is loaded.
3. On each action and the recurring job, check the pinned inputs (Admin > Orchestration Actions, Admin > Recurring Jobs): the ConnectionInfo name, the catalog item sys_id on the Open actions (already set to the update set's item; change it for a hand-built item, clear it for the Table API path), and optionally the CloudBolt base URL and the CMDB table (Close on Completion). On the sync job set `cloudbolt_approver_username` to a CloudBolt user with approve rights if you want the CloudBolt approval history to name someone.
4. Enable, in this order: Close on Completion, Close on Denial, Open on Approval, Approval Sync, and finally Open on Submit. Enabling Open on Submit stops CloudBolt's approver emails for every order; from then on ServiceNow decides. Scope Open on Submit (groups, blueprints) to the orders ServiceNow should govern; Open on Approval stays unconstrained and follows that scope.
5. If the ServiceNow integration under Admin > ServiceNow has its "order" hook enabled, disable it; otherwise each order also creates a bare `sc_request`.
6. Keep CloudBolt's CMDB integration (Post-Provision CI creation) enabled. Close on Completion finds each server's CI by `serial_number` or `asset_tag` equal to the resource handler's VM id, falling back to the hostname.

Resources that are not servers (buckets, clusters) are attached to the RITM only when something sets the custom field `servicenow_ci_sys_id` on the resource. Blueprints that create their own CIs can do so in their build plugin.

## Verify on your instance

The ServiceNow API shapes were taken from the REST API Explorer references and ServiceNow community articles, not tested against a live instance. Check these before going to production:

- `POST /api/sn_sc/servicecatalog/items/<sys_id>/order_now` returns `result.sys_id` and `result.number` (or `request_id` / `request_number`) for the REQ. With two-step checkout it returns `cart_id`, and `POST /api/sn_sc/servicecatalog/cart/submit_order` returns `request_id` / `request_number`.
- A multi-row variable set is accepted in `variables` as a JSON string of row objects keyed by column name.
- `sc_req_item.state` values: 3 Closed Complete, 4 Closed Incomplete, 7 Closed Skipped. `approval` values: `requested`, `approved`, `rejected`. Both live in `sys_choice`.
- `task_ci` columns `task` and `ci_item`.
- `sc_req_item.price`, `recurring_price` and `recurring_frequency` (choice values `daily`, `weekly`, `monthly`, `yearly`) and `sc_request.price` accept the CloudBolt net rate; an hourly CloudBolt rate is passed as a monthly amount (730 hours), a one-time rate as `price` only.
- `sysapproval_approver` rows inserted with `sysapproval`, `document_id`, `source_table`, `approver` and `state=requested` appear under My Approvals and are settable from there.

Verified on a CloudBolt 2026.3 appliance with a ServiceNow ITSM integration (2026-10-13, Table API path): an auto-approved Azure Resource Group order opened REQ/RITM through Post-Order Approval, Close on Completion closed the RITM complete, and the identifiers landed on the order item and the resource.
- The integration user can write `requested_for`, `correlation_id`, `short_description`, `description`, `work_notes`, `close_notes`, `state` and `cmdb_ci` on `sc_req_item`.

## Troubleshooting

- Order submitted but no RITM: open the order's history. A failure in Open on Submit is shown to the user at submission; the order stays pending with no approver email.
- RITM shows the integration user as Requested for: the user lacks `glide.sc.req_for.roles` permission, or the owner's email or username does not match a `sys_user`. Set `servicenow_requested_for_miss` to `fail` to refuse such orders.
- Orders never leave Pending and the RITM's Approval reads "Not requested": the catalog item has no flow and no approvers are pinned, so nobody was asked. Attach the flow, or pin approvers, then cancel and resubmit the order; a flow does not run for a RITM that already exists.
- Orders stay pending after approval in ServiceNow: check the sync job's log and the `servicenow_approval_table` and `servicenow_approval_field` pins. Open the REQ and the RITM and see which one carries the `approval` value your workflow or flow sets; the RITM form does not show Approval by default, so use the list view or add the field to the form.
- The ServiceNow Request and RITM links show the full URL as their text. CloudBolt's URL parameter has no separate display label; the form URL keeps the number visible at the end.
- No CI attached at completion: the CMDB integration did not create the CI, or it stored the VM id in a different column; adjust `find_server_ci` in the shared module.
