# Add Parent Blueprints to Group

Post Group Creation orchestration action. When a group is created under a parent, it walks the parent chain to the root and gives the new group explicit Deploy permission on every blueprint that any ancestor is explicitly permitted to deploy. Blueprints marked "Any group can deploy" are ignored, because the new group can already order them. Top-level groups are a no-op.

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-1lp8xqo3 | Add Parent Blueprints to Group (Post Group Creation, enabled) |
| Plugin | OHK-tswevdir | Add Parent Blueprints to Group |

## Prerequisites
None. The action reads and writes CloudBolt's own blueprint group permissions; no external systems are involved.

## Setup
1. Sync the repository; the action imports enabled.
2. Create a subgroup under a group that holds explicit Deploy permission on a blueprint, then open that blueprint's Groups tab: the subgroup is listed with Deploy checked.

## Notes
- An ancestor counts as able to deploy a blueprint when it has either a Manage or a Deploy permission row on it, which is the same test CloudBolt uses for `groups_with_deploy_permission`. Only Deploy is granted to the new group; Manage is never copied.
- Idempotent: a permission that already exists is reported as already present and left alone.
- Ships with `continue_on_failure: true`. The group row is already saved when this hook fires, so a failure here is logged without surfacing an error on the group-creation form.
- Groups created before the action is enabled are not back-filled. Re-saving an existing group does not trigger it; only creation does.
