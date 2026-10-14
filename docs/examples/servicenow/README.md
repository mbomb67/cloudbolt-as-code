# ServiceNow update set: CloudBolt Automation Request

`cloudbolt-automation-request-update-set.xml` is a ServiceNow update set (exported from a Yokohama-family instance) that creates the one generic catalog item the ServiceNow request actions order, its variables, and a generic flow, so nobody has to build them by hand. It is the ServiceNow half of [../../servicenow-request-setup.md](../../servicenow-request-setup.md).

## Contents
| Record | Table | Count |
|---|---|---|
| Catalog item `CloudBolt Automation Request`, bound to the flow | `sc_cat_item` | 1 |
| Variable sets `CloudBolt Request Details` (single-row) and `CloudBolt Items` (multi-row) | `item_option_new_set` | 2 |
| Variables (13 request details, 6 item columns) | `item_option_new` | 19 |
| Item-to-set links | `io_set_item` | 2 |
| Flow `CloudBolt Automation Request` (Service Catalog trigger, Update Record: Approval = Approved) | `sys_hub_flow` | 1 |

The flow approves every RITM the moment it exists, so orders proceed with no approver involved. It references no user or group, which is what makes it import anywhere. Approval policy is the customer's: insert an **Ask For Approval** step (and, if wanted, an If on `cloudbolt_approval` so orders CloudBolt already approved skip it) in front of the Update Record node, routing on the `cloudbolt_*` variables. Keep the rule from the setup doc: the flow records its decision in the RITM's Approval field and never closes the RITM; CloudBolt fulfils and closes it.

## Import
1. The target instance needs the plugin "Flow Designer support for the Service Catalog" (`com.glideapp.servicecatalog.flow_designer`) for the Service Catalog trigger; install it first if Flow Designer does not offer that trigger.
2. System Update Sets > Retrieved Update Sets > **Import Update Set from XML**, choose the file.
3. Open the retrieved set, **Preview Update Set**. One warning is expected: the item's Category points at the source instance's "IT Services Catalog" category. Accept the remote update; you set the category after commit.
4. **Commit Update Set**.
5. Open the catalog item (Maintain Items), set Category to one of yours, Update. Sys_ids are preserved by the update set, so the item's sys_id is the same on every instance that imports this file, and it is already the default pinned on both Open actions in CloudBolt.
6. Open the flow in Flow Designer and confirm it is Active; extend it with your approval policy when ready.

## Re-exporting
Make the changes with an update set current, complete the set, Export to XML, and replace this file. Saving the item while the set is current captures its Flow binding; saving the flow in Flow Designer captures the flow.
