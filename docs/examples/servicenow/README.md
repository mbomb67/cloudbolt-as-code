# ServiceNow update set: CloudBolt Automation Request

`cloudbolt-automation-request-update-set.xml` is a ServiceNow update set (exported from a Yokohama-family instance) that creates the one generic catalog item the ServiceNow request actions order, with its variables, so nobody has to build it by hand. It is the ServiceNow half of [../../servicenow-request-setup.md](../../servicenow-request-setup.md).

## Contents
| Record | Table | Count |
|---|---|---|
| Catalog item `CloudBolt Automation Request` | `sc_cat_item` | 1 |
| Variable sets `CloudBolt Request Details` (single-row) and `CloudBolt Items` (multi-row) | `item_option_new_set` | 2 |
| Variables (13 request details, 6 item columns) | `item_option_new` | 19 |
| Item-to-set links | `io_set_item` | 2 |

Not included on purpose: the approval flow. Approval policy belongs to the customer's ServiceNow team; build it against the variables this item carries, following the reference flow in the setup doc. The item is exported with Process Engine and Flow empty.

## Import
1. System Update Sets > Retrieved Update Sets > **Import Update Set from XML**, choose the file.
2. Open the retrieved set, **Preview Update Set**. One warning is expected: the item's Category points at the source instance's "IT Services Catalog" category. Accept the remote update; you set the category after commit.
3. **Commit Update Set**.
4. Open the catalog item (Maintain Items), set Category to one of yours, Update. Sys_ids are preserved by the update set, so the item's sys_id is the same on every instance that imports this file; right-click the form header > Copy sys_id.
5. Build and attach the approval flow (setup doc, "Build the approval flow"), then pin the item's sys_id on both Open actions in CloudBolt.

## Re-exporting
Make the changes with an update set current, complete the set, Export to XML, and replace this file. Detach the flow from the item before the capture so the export never references a flow that only exists on your instance.
