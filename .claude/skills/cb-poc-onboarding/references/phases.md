# Phases 2 to 9

Each phase: `onb_status` first, do the steps, record the items with `onb_set_item_status`, close with a short report. Where a step says "UI", give the user the `onb_ui_link` link and the documentation link, then verify with a tool. Steps marked **toolkit later** will become tools in later toolkit versions; until then they are UI steps.

## Phase 2: Environments (P3, 1.1.4, 1.2.1)

One or two environments per handler, each able to build the Custom Server with the fewest questions on the order form.

1. **Import environments** (UI, toolkit later). Link `handler_import_locations:RH-...`. What an environment maps to: AWS a region plus one VPC (its subnets are imported with it); Azure a location inside the subscription (import subscriptions first on the handler page when the handler was created at tenant level); vCenter a cluster; SCVMM a host cluster; GCP a project; Nutanix a cluster; OpenStack a location. Pick one, two at most, per handler. Verify with `onb_status`: each handler lists its environments.
2. **Network** (UI). AWS and Azure: the environment's Networks tab, Sync subnets, then pick one subnet for NIC 1. vCenter: on the handler, sync networks, then on the environment add one network to NIC 1. Verify: `onb_status` env `networks` is at least 1.
3. **Image and OS build** (UI, toolkit later). On the handler's images (templates, AMIs) page, import one Linux and, if the POC uses Windows, one Windows image into an OS build, in the environment's region or location, and attach the OS build to the environment. For 1.2.1, use the same OS build name on both handlers (one "Windows 2022" or "Ubuntu 22.04" build with one image per handler) so one blueprint deploys to both clouds. Verify: env `osBuilds` is at least 1.
4. **Trim the order form** (UI, toolkit later). Environment > Parameters tab and the technology tab. A required parameter with exactly one option disappears from the order form; an optional one must be removed instead. Aim for the minimal form:

   | Technology | Show the user | Pin to one value | Remove |
   |---|---|---|---|
   | AWS | `instance_type` (2 or 3 sizes), the subnet only if more than one is wanted | `key_name`, `sec_groups`, `delete_ebs_volumes_on_termination`, `ebs_volume_type` | `aws_availability_zone`, `aws_elastic_ip`, `iops`, `aws_host`, `aws_host_group` |
   | Azure | `node_size` (constrain to 2 or 3), `password_arm` for Windows | `resource_group_arm` (one POC group), `storage_account_type_arm`, `create_public_ip`, `availability_set_arm` or `availability_zone_arm` (none) | `spot_instance_arm`, eviction fields, `max_price_arm`, `platform_fault_domain_count`, `custom_storage_account_arm`, `storage_account_arm`, `encryption_at_host`, `security_type_arm`, `security_encryption_type_arm`, `os_disk_size_arm`, `resource_group_arm_template`, `enable_accelerated_networking` |
   | vCenter | `cpu_cnt`, `mem_size` (short option lists) | `vmware_datastore`, `vmware_disk_type` | `vmware_resourcepool`, `nsx_scope`, `nsx_edge` |

   Keep every special field (`aws_region`, `vpc_id`, `node_location`, `vmware_cluster`) and the size field; provisioning fails without them. Azure Windows needs `password_arm` on the form.
5. **Hostname template** (UI) for 1.1.4: on the environment's Parameters tab add `hostname_template`, for example `poc-{{ group.name|slice:":3" }}-{{ os_build.os_family.name|slice:":3" }}-#` without the spaces the Markdown shows; the order form previews it. Documentation: [hostname specification](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/hostname-specification).
6. Record P3 `done` when every handler has a verified connection and at least one environment with an image and a network.

## Phase 3: First order (P4, 1.1.1, 1.1.3, 1.2.3, 3.2.3)

Fully through MCP. For each environment:

1. `fetch_catalog_items` with `name: "Custom Server"`. If the blueprint is missing or not orderable, `onb_status.customServerBlueprint` has the restore link.
2. `fetch_orderable_groups` with the blueprint id. One group: use it. Several: ask. None: the user's group has no deploy permission; `manage_blueprint_permissions` with `grant_action: list` shows who has, and the user decides who should.
3. `fetch_blueprint_schema`. For the server item, `fetch_orderable_environments`, then `fetch_blueprint_item_parameters` for the chosen environment, then `fetch_blueprint_parameter_options` for each parameter with dynamic options. Choose the smallest size.
4. Echo the whole order verbatim (group, resource name, item slug, environment, OS build, every parameter) and wait for yes.
5. `order_catalog_item`. Poll `fetch_order`; if `PENDING`, the group requires approval: `approve_order` (or that is test 2.4.1). Then `fetch_job` on the provision job.
6. On `FAILURE`: `onb_failure_trace` with the order id, then [troubleshooting.md](troubleshooting.md).
7. Record P4 and 1.1.1 `done` with the order id. For 1.1.3, add a post-provision action first (Phase 8 brings the Hello World scripts; a Content Library action works too) and order once more.
8. `fetch_server_list` with `group_name` shows both clouds' servers in one list (1.2.3). `fetch_server_actions` on one server, then `run_server_action` for power off, on and reboot, polling each job (3.2.3; `set_server_power_schedule` is Phase 7). Documentation: [lifecycle actions](https://academy.university.cloudbolt.io/guides/019403d1-422e-7471-826c-4a6c12820e2a/tasks/0193e483-6d74-7c94-a8c4-464683bd3b51).

## Phase 4: Self-service (1.1.2, 1.2.2, 2.5.1, 4.3.1)

1. **A user-defined parameter** (UI, toolkit later): `onb_ui_link new_parameter`, create for example `application_tier` (single select: web, app, db), add it to the environment or the blueprint with those options, order again and check the value on the server (1.1.2). [Parameters](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/parameters).
2. **Generated options** (4.3.1): Phase 8 syncs a sample generated-options action from this repository; or write a three-line `generate_options_for_<parameter>` orchestration action in the UI. [Generated options](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/options-generated-using-orchestration-actions).
3. **Tags** (1.2.2, 2.5.1): on each handler's Tags tab map `Group`, `Owner`, `OS Family` and a `cost_center` parameter to cloud tags; add `cost_center` to the group with a value; order; check the tags in both clouds. [Tags](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/tags).

## Phase 5: RBAC and governance (2.3.1 to 2.3.4, 2.4.1, 2.4.2, 2.5.2)

1. **Groups and users** (UI, toolkit later): `onb_ui_link groups` to add a POC group (and a child group); `new_user` for two local test users, a requestor and an approver, with roles on the group; the passwords are set on each user's `user_edit:USR-...` page by the admin, never in chat. `fetch_groups` and `fetch_group_details` confirm memberships. [Groups and permissions](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/groups-permissions).
2. **Visibility** (2.3.1): the requestor logs in and sees only their own servers; change the role to Resource Admin and they see the group's. The user does the logins; the agent confirms the memberships.
3. **Blueprint entitlement** (2.3.2): `manage_blueprint_permissions` to grant one group DEPLOY on a blueprint and `fetch_orderable_groups` to prove the other group cannot order it. [Blueprint group permissions](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/group-permissions-for-blueprints).
4. **Environment entitlement** (2.3.3): assign one environment to one group (UI, environment page); `fetch_orderable_environments` as each user proves it. [Ordering servers and environments](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/ordering-servers-environments).
5. **Custom role** (2.3.4): `onb_ui_link roles`, create a role with a few permissions, assign it; the user logs in to check. [Custom roles](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/groups-permissions/a/h2__322854237).
6. **Approvals** (2.4.1, 2.4.2): the requestor orders (through the UI or `order_catalog_item` as that user); `fetch_orders_pending_my_approval` and `approve_order` as the approver. For the day-2 case, the Resize VM server action needs "Requires approval" ticked (UI, `server_actions`; toolkit later); `run_server_action` returns an order instead of a job.
7. **Quota** (2.5.2): `onb_ui_link group_quotas:GRP-...`, set 3 servers; the fourth order is refused. [Quotas](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/groups-permissions/a/h2_878037613).

## Phase 6: Authentication (2.1.1, 2.1.2, 2.1.3, 2.2.1)

1. **LDAP utility** (UI; the service-account password is entered there): `onb_ui_link new_ldap`; pre-brief server, port (389 or 636), protocol, base DN, bind account DN. After save, `ldap_verify:<id>`; on failure `onb_recent_errors` with `onb_log: authentication`. The user logs in with an AD account (2.1.1). [LDAP authentication](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/ldap-authentication).
2. **LDAP mapping** (UI, toolkit later): `ldap_mappings:<id>`, map the AD group DN from P10 to the POC group with a role; an AD member logs in and lands in the group (2.1.2). [User permission synchronization](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/user-permission-and-attribute-synchronization/a/h2_1939259882).
3. **SSO** (2.1.3): UI only with the customer's SSO SME: `onb_ui_link sso`, metadata exchange, assign users, log in. Skip and record not applicable when P9 is not met. [SSO and SAML](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/single-sign-on-and-saml).
4. **Branded portal** (2.2.1): needs the DNS record from P8; `onb_ui_link portals`; logos and colours are a UI job. [Branded portals](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/branded-portals).

## Phase 7: Lifecycle (3.1.1 to 3.1.4, 3.2.1)

1. **Power schedule** (3.1.1): add the `power_schedule` parameter to an environment (UI, Parameters tab), order a server choosing a schedule, or set one directly with `set_server_power_schedule` and read it back with `fetch_server_power_schedule`. [Power scheduling](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/power-scheduling-servers/a/h2_1163015685).
2. **Expiration** (3.1.2): add `expiration_date` to the environment with a constraint (for example at most 30 days out), order with a date, and confirm the Expire Servers recurring job is enabled (`onb_ui_link recurring_jobs`). [Server expiration](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/server-expiration).
3. **Self-service decommission** (3.1.3): the owner deletes the server from the portal, or the agent runs the Delete action with `run_server_action` and polls the job.
4. **Onboard existing servers** (3.1.4): the handler's Servers tab shows discovered servers in Unassigned; `fetch_server_list` with `group_name: Unassigned`, then `run_server_action` with the Change Attributes action to set group and owner; a group member then sees them in `fetch_server_list`.
5. **Resize** (3.2.1): the Resize VM server action (Content Library if absent, Phase 8); `run_server_action`, poll, and confirm the size in the cloud console.

## Phase 8: Extensibility (1.1.3, 4.1.2, 4.2.1 to 4.2.6, 4.3.1 to 4.3.3)

All Content Library installs need P5. Installs are UI steps (`onb_ui_link content_library`; toolkit later).

1. **Jupyter Notebooks UI extension** (4.2.1): Content Library > UI Extensions > install, then follow its documentation cell; Admin > Admin Extensions shows it. [Forge Jupyter XUI](https://cloudboltsoftware.github.io/cloudbolt-forge/sites/NewJupyterXUI/jupyter.html), [UI extensions](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/ui-extensions).
2. **Recurring job** (4.2.2): install "Cleanup zip files from tmp", create a zip under /var/tmp on the appliance, set the cron schedule, run it (the testing toolkit's `run_recurring_job` if installed). [Recurring jobs](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/recurring-jobs).
3. **Rules** (4.2.3): `onb_ui_link rules`, enable "Email Group Admins when quota usage over threshold", set a group quota below its server count, execute rules, check the email (needs P7). [Rules](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/rules).
4. **CIT** (4.2.4): `onb_ui_link cit_tests`, build a test from the successful Custom Server order, run it. [Continuous infrastructure testing](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/continuous-infrastructure-testing).
5. **Inbound webhook** (4.2.5): sync a sample echo webhook from this repository (`sync_from_source_control_repo` with the `webhooks/IWH-...` path) or create the documented sample; the user posts JSON with curl or Postman and gets it back. [Inbound webhooks](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/inbound-webhooks/).
6. **Email template** (4.2.6): `onb_ui_link email_templates`, edit Order Complete, order, read the email (needs P7). [Email templates](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/customizing-email-templates).
7. **Scripts** (1.1.3, 4.3.2, 4.3.3, 4.1.2): a Hello World Remote Script (PowerShell for Windows, Bash for Linux) as a build item on a copy of the Custom Server blueprint, ordered again; the Ad-Hoc Script server action from the Content Library for 4.1.2. Guest credentials live on the OS build or server (UI). [Remote scripts](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/remote-scripts).
8. **Generated options** (4.3.1): see Phase 4.
9. Terraform (4.3.4) and Ansible (3.2.2, 4.3.5) carry weight 0: do them only if P11 or P12 are met and time allows; otherwise record not applicable.

## Phase 9: Wrap-up

1. `onb_status` for live evidence (orders, handlers, environments, groups, LDAP).
2. For every item in [test-plan-map.md](test-plan-map.md), pair the recorded status (`onb_status.pocItems`) with the evidence. Items with no record and no evidence are `pending`.
3. Report the table by category with weights, the prerequisites, the pending items with their blocker and owner, and the follow-ups raised during the week. The customer fills the workbook's score column from it.
