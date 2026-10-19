# Phases 2 to 9

Each phase: `onb_status` first, do the steps, record the items with `onb_set_item_status`, close with a short report. Echo and confirm before every write. Asynchronous tools return a job id: poll `fetch_job` and read `outputs`.

## Phase 2: Environments (P3, 1.1.4, 1.2.1)

One or two environments per handler, each able to build the Custom Server with the fewest questions on the order form.

1. **Locations.** `onb_handler_locations` with the handler. Show the user the locations not yet imported and ask for one or two. AWS: also pass `onb_region` for the chosen region to list its VPCs, or let the tool use the region's default VPC. Azure: a tenant-level handler must first import a subscription on the handler page (`handler_import_locations:RH-...` link); the tool says so.
2. **Import.** `onb_import_environments` with the handler and the locations (`<region>` or `<region>|<vpc-id>`, a location, a cluster). AWS subnets come with the environment, Azure subnets are synced, vCenter networks of the cluster are attached to NIC 1. Azure fills its parameters in a separate job; wait a minute before step 4. Verify: `onb_status` lists the environments with `networks` at least 1.
3. **Image.** Confirm the `onb_sync_images` job from Phase 1 finished (`fetch_job`); `onb_status` shows `cachedImages` per handler. Ask which OS the POC will build (one Linux image, and Windows when the plan needs PowerShell). `onb_import_os_build` with the handler and `onb_image` empty lists the cached images for the environment's region or location; a partial name (`ubuntu`, `Windows Server 2022`) narrows the list. Then call it with the chosen AMI id, Azure `publisher:offer:sku:version` or vCenter template name and an OS build name. An AMI id that is not cached is fetched on the spot; an empty Azure or vCenter cache is refreshed without the whitelist first. For 1.2.1 call it on the second handler with the **same** OS build name so one blueprint deploys to both clouds; when the tool reports it attached the image to an existing build of another name, use that name for the other handler. Verify: `osBuilds` at least 1 on each environment.
4. **Trim the order form.** `onb_trim_environment` with the environment (profile `poc_minimal`). Read the `actions` to the user: which fields were pinned and to what (key pair, security group, resource group, datastore, subnet), which were removed, which sizes remain. Change a pin with `onb_pin field=value` or the size list with `onb_pin instance_type=a|b|c`; keep a field with `onb_keep`. On Azure the Windows password field stays. Target: `visibleNow` is the size (and on Azure the password).
5. **Custom Server and hostname template.** `onb_configure_custom_server` with a hostname template for 1.1.4 (for example `poc-#`; the documented template language is in [hostname specification](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/hostname-specification)). Pass `onb_environments` to put the template on the environments instead of the server tier.
6. A wrong import: `onb_remove_environment` (confirm first). Record P3 `done` when every handler is verified and has at least one environment with an image and a network.

## Phase 3: First order (P4, 1.1.1, 1.2.3, 3.2.3)

Fully through MCP. For each environment:

1. `fetch_catalog_items` with `name: "Custom Server"`. Missing or not orderable: `onb_configure_custom_server`.
2. `fetch_orderable_groups` with the blueprint id. One group: use it. Several: ask. None: the user's group has no deploy permission; `manage_blueprint_permissions` with `grant_action: list` shows who has, and the user decides who should.
3. `fetch_blueprint_schema`. For the server item, `fetch_orderable_environments`, then `fetch_blueprint_item_parameters` for the chosen environment, then `fetch_blueprint_parameter_options` for each parameter with dynamic options. Choose the smallest size.
4. Echo the whole order verbatim (group, resource name, item slug, environment, OS build, every parameter) and wait for yes.
5. `order_catalog_item`. Poll `fetch_order`; if `PENDING`, the group requires approval: `approve_order` as an approver (or that is test 2.4.1). Then `fetch_job` on the provision job.
6. On `FAILURE`: `onb_failure_trace` with the order id, then [troubleshooting.md](troubleshooting.md).
7. Record P4 and 1.1.1 `done` with the order id.
8. `fetch_server_list` with `group_name` shows both clouds' servers in one list (1.2.3). `fetch_server_actions` on one server, then `run_server_action` for power off, on and reboot, polling each job (3.2.3). [Lifecycle actions](https://academy.university.cloudbolt.io/guides/019403d1-422e-7471-826c-4a6c12820e2a/tasks/0193e483-6d74-7c94-a8c4-464683bd3b51).

## Phase 4: Self-service (1.1.2, 1.2.2, 2.5.1, 4.3.1)

1. **A user-defined parameter** (1.1.2): `onb_parameter` with name `application_tier`, label, type STR, options `web`, `app`, `db`, targets the POC environments (or the POC group, Phase 5). Order again and check the value on the server (`fetch_server_details`). [Parameters](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/parameters).
2. **Generated options** (4.3.1): the POC Server blueprint (Phase 8) carries `poc_application_tier` whose options come from a Python action; ordering it shows the dropdown. [Generated options](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/options-generated-using-orchestration-actions).
3. **Tags** (1.2.2, 2.5.1): `onb_tag_mapping` on each handler with the same lines, for example `group=Group`, `owner=Owner`, `os_family=OSFamily`, `cost_center=CostCenter`. `cost_center` first needs `onb_parameter` with `onb_param_show_on_servers=true` and a value on the group (`onb_param_targets` the group, `onb_param_options` one value). Order a server in each cloud and check the tags in both consoles. [Tags](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/tags).

## Phase 5: RBAC and governance (2.3.1 to 2.3.4, 2.4.1, 2.4.2, 2.5.2)

1. **Group** (2.3.3, 2.5.2): `onb_group` with a name, the environments it may use, `onb_group_auto_approval=false` and `onb_group_quota_servers=3`. [Groups and permissions](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/groups-permissions).
2. **Users** (2.3.1, 2.4.1): `onb_user` twice, a requestor (`onb_user_roles: requestor`) and an approver (`approver`), in that group. Send the admin each `setPasswordUrl`; passwords are set in the UI. The users log in; the requestor sees only their own servers, and after `onb_user` with `resource_admin` they see the group's.
3. **Blueprint entitlement** (2.3.2): `manage_blueprint_permissions` to grant the POC group DEPLOY on a blueprint that is not open to every group; `fetch_orderable_groups` as each user proves it. [Blueprint permissions](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/group-permissions-for-blueprints).
4. **Environment entitlement** (2.3.3): the environments passed to `onb_group`; `fetch_orderable_environments` as each user proves it. [Ordering environments](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/ordering-servers-environments).
5. **Custom role** (2.3.4): UI, `onb_ui_link roles`; then `onb_user` with that role name. [Custom roles](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/groups-permissions/a/h2__322854237).
6. **Approvals** (2.4.1, 2.4.2): the requestor orders; `fetch_orders_pending_my_approval` and `approve_order` as the approver. For the day-2 case `onb_action_approval` with `Resize VM` (install it first from the Content Library in Phase 8 if absent); `run_server_action` as the requestor returns an order instead of a job.
7. **Quota** (2.5.2): the quota set in step 1; the fourth order is refused. [Quotas](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/groups-permissions/a/h2_878037613).

## Phase 6: Authentication (2.1.1, 2.1.2, 2.1.3, 2.2.1)

1. **LDAP utility** (2.1.1): UI, because the service-account password is entered there: `onb_ui_link new_ldap`; pre-brief server, port (389 or 636), protocol, base DN, bind account. After save, `onb_verify_ldap`; on failure its `hints` and `onb_recent_errors` with `onb_log: authentication`. A directory user logs in. [LDAP authentication](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/ldap-authentication).
2. **LDAP mapping** (2.1.2): `onb_ldap_mapping` with the AD group DN from P10, the POC group and the roles; a member logs in and `fetch_group_details` lists them. [User permission synchronization](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/user-permission-and-attribute-synchronization/a/h2_1939259882).
3. **SSO** (2.1.3): UI only with the customer's SSO SME: `onb_ui_link sso`. Skip and record not applicable when P9 is not met. [SSO and SAML](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/single-sign-on-and-saml).
4. **Branded portal** (2.2.1): needs the DNS record from P8; `onb_ui_link portals`. [Branded portals](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/branded-portals).

## Phase 7: Lifecycle (3.1.1 to 3.1.4, 3.2.1)

1. **Power schedule** (3.1.1): `onb_parameter` with name `power_schedule` and the POC environments as targets; order a server choosing a schedule, or `set_server_power_schedule` on an existing one and `fetch_server_power_schedule` to read it back. [Power scheduling](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/power-scheduling-servers/a/h2_1163015685).
2. **Expiration** (3.1.2): `onb_parameter` with name `expiration_date`, targets the environments, `onb_param_max=30` (days). The Expire Servers recurring job must be enabled (`onb_status` next steps say when it is not; `onb_ui_link recurring_jobs`). Order with a date. [Server expiration](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/server-expiration).
3. **Self-service decommission** (3.1.3): the owner deletes the server from the portal, or `run_server_action` with the Delete action and poll.
4. **Onboard existing servers** (3.1.4): `fetch_server_list` with `group_name: Unassigned`, then `run_server_action` with Change Attributes to set group and owner; a group member then sees them. Skip when the clouds hold no existing VMs.
5. **Resize** (3.2.1): `onb_install_content_library_item` with collection `server-actions` and `Resize VM` when absent, then `run_server_action`, poll, and confirm the size in the cloud console.

## Phase 8: Extensibility (1.1.3, 4.1.2, 4.2.1 to 4.2.6, 4.3.1 to 4.3.3)

Content Library installs need P5. Each `onb_install_content_library_item` call is a job; with `onb_cl_item` empty it lists the collection.

1. **POC Server blueprint** (1.1.3, 4.3.1, 4.3.2, 4.3.3): `sync_from_source_control_repo` with `sync_branch: main` and `sync_paths` `blueprints/BP-x78p48gz` (the Hello World scripts and the generated-options action come with it) and `webhooks/IWH-f16qxnhm`. The blueprint is open to every group, so no permission restore is needed. Guest credentials must be on the OS build or server (SSH on 22, WinRM on 5985 or 5986 from the appliance). Order it through MCP like the Custom Server: the application tier dropdown is Python-generated, and the job log of the Hello World item shows the message printed inside the guest. [Remote scripts](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/remote-scripts).
2. **Ad-Hoc Script** (4.1.2): `onb_install_content_library_item` with `server-actions` and the Ad-Hoc Script action; `run_server_action` with a one-line script; the output is in the job.
3. **Jupyter Notebooks UI extension** (4.2.1): `onb_install_content_library_item` with `ui-extension-packages`; the web server needs a restart before the page opens (the tool says so), then follow the extension's documentation cell. [UI extensions](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/ui-extensions).
4. **Recurring job** (4.2.2): `onb_install_content_library_item` with `recurring-jobs` and the tmp cleanup job; the user creates a zip under /var/tmp on the appliance; set the schedule in the UI (`onb_ui_link recurring_jobs`); `run_recurring_job` runs it now. [Recurring jobs](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/recurring-jobs).
5. **Rules** (4.2.3): `onb_ui_link rules`, enable "Email Group Admins when quota usage over threshold", set the POC group's quota below its server count with `onb_group`, execute rules on that page, check the email (needs P7). [Rules](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/rules).
6. **CIT** (4.2.4): `onb_cit_from_order` with the successful Custom Server order, then `run_cit_tests` with the test id and poll; each run provisions and decommissions a server. [Continuous infrastructure testing](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/continuous-infrastructure-testing).
7. **Inbound webhook** (4.2.5): the POC Echo webhook from step 1 answers at `/api/v3/cmp/inboundWebHooks/poc-echo/run/`; the user posts JSON with curl or Postman using their API token and gets it back with their username. [Inbound webhooks](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/inbound-webhooks/).
8. **Email template** (4.2.6): UI, `onb_ui_link email_templates`, edit Order Complete, order, read the email (needs P7). [Email templates](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/customizing-email-templates).
9. Terraform (4.3.4) and Ansible (3.2.2, 4.3.5) carry weight 0: do them only if P11 or P12 are met and time allows; otherwise record not applicable.

## Phase 9: Wrap-up

1. `onb_scorecard`. Read the `pending` items to the user with their evidence line; for each, record `done` with the evidence or `not_applicable` with the reason using `onb_set_item_status`.
2. `onb_scorecard` again and present it: categories, items with weight and status, points possible (and applicable), prerequisites.
3. List the follow-ups raised during the week that were out of scope. The customer fills the workbook's score column from the table.
