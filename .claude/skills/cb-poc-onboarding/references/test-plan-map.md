# POC test plan map

Every item of the CloudBolt CMP POC Test Plan with its weight (the workbook multiplies a 0 to 5 score by it), the phase that covers it, how the agent does or verifies it, the evidence to record, the documentation link, and whether the customer may record it as not applicable. Prerequisites are in [bootstrap.md](bootstrap.md).

Status values for `onb_set_item_status`: `done`, `pending`, `skipped`, `not_applicable` (reason required for the last two).

## Self-service (weights total 35)

| Id | Item | Weight | Phase | How | Evidence | Docs | Skippable |
|---|---|---|---|---|---|---|---|
| 1.1.1 | Provision a blueprint from the catalog as a non-admin | 5 | 3, 5 | MCP order flow; the requestor user orders too | Order id, server | [Blueprints](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/blueprints) | No |
| 1.1.2 | User-defined parameter at order time | 5 | 4 | Parameter on env or blueprint (UI), order through MCP | Parameter name, order id, value on server | [Parameters](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/parameters) | No |
| 1.1.3 | Post-provisioning automation from the blueprint | 5 | 8 | Hello World build item, order again | Order id, job output | — | No |
| 1.1.4 | Hostname template with preview | 5 | 2 | `hostname_template` on the environment (UI) | Hostname of the built server | [Hostname specification](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/hostname-specification) | No |
| 1.2.1 | One blueprint deploys to two clouds | 5 | 2, 3 | Shared OS build with one image per handler; two orders | Two order ids, two servers | — | Yes, single-cloud POC |
| 1.2.2 | Cross-cloud tagging consistency | 5 | 4 | Tag mappings on both handlers (UI); order; check tags | Tags on both servers | [Tags](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/tags) | Yes, single-cloud POC |
| 1.2.3 | Unified visibility across clouds | 5 | 3 | `fetch_server_list` by group | The list | — | Yes, single-cloud POC |

## RBAC and governance (weights total 39)

| Id | Item | Weight | Phase | How | Evidence | Docs | Skippable |
|---|---|---|---|---|---|---|---|
| 2.1.1 | LDAP or AD authentication | 3 | 6 | LDAP utility (UI, password there), verify, AD login | Login in authentication log | [LDAP](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/ldap-authentication) | Yes, when P10 unmet |
| 2.1.2 | Group and role from AD group membership | 3 | 6 | LDAP mapping (UI) | AD user's group and role | [User permission sync](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/user-permission-and-attribute-synchronization/a/h2_1939259882) | Yes, when P10 unmet |
| 2.1.3 | SAML2 SSO | 2 | 6 | UI with the SSO SME | SSO login | [SSO](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/single-sign-on-and-saml) | Yes, when P9 unmet |
| 2.2.1 | Branded portal per tenant | 3 | 6 | Portal (UI) on the DNS record | Portal URL loads branded | [Branded portals](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/branded-portals) | Yes, when P8 unmet |
| 2.3.1 | Visibility constrained by group role | 4 | 5 | Requestor vs Resource Admin logins | What each user sees | [Groups and permissions](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/groups-permissions) | No |
| 2.3.2 | Blueprint entitlement by group | 4 | 5 | `manage_blueprint_permissions`, `fetch_orderable_groups` | Which group can order | [Blueprint permissions](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/group-permissions-for-blueprints) | No |
| 2.3.3 | Environment entitlement by group | 4 | 5 | Environment group (UI), `fetch_orderable_environments` | Which group sees it | [Ordering environments](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/ordering-servers-environments) | No |
| 2.3.4 | Custom role | 3 | 5 | Role (UI), assignment, login | Permissions seen | [Custom roles](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/groups-permissions/a/h2__322854237) | No |
| 2.4.1 | Order approval | 3 | 5 | Requestor orders, `approve_order` as approver | Order id, approval | — | No |
| 2.4.2 | Approval on a day-2 action | 3 | 5 | Resize VM requires approval (UI), `run_server_action` returns an order | Order id | — | No |
| 2.5.1 | Tagging policy from a parameter | 5 | 4 | `cost_center` parameter on the group, tag mapping | Tag in the cloud | [Tags](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/tags) | No |
| 2.5.2 | Group quota | 2 | 5 | Quota 3 servers (UI), fourth order refused | Refused order | [Quotas](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/groups-permissions/a/h2_878037613) | No |

## Resource management (weights total 26)

| Id | Item | Weight | Phase | How | Evidence | Docs | Skippable |
|---|---|---|---|---|---|---|---|
| 3.1.1 | Power schedules | 5 | 7 | `power_schedule` parameter, `set_server_power_schedule` | Schedule read back, power events | [Power scheduling](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/power-scheduling-servers/a/h2_1163015685) | No |
| 3.1.2 | Leases (expiration) | 5 | 7 | `expiration_date` with constraint, Expire Servers job | Expiration on server, job enabled | [Server expiration](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/server-expiration) | No |
| 3.1.3 | Self-service decommission | 4 | 7 | Owner deletes, or `run_server_action` Delete | Job id, server gone | — | No |
| 3.1.4 | Onboard existing cloud resources | 5 | 7 | Change Attributes on Unassigned servers | Servers in the group | — | Yes, when no existing VMs |
| 3.2.1 | Resize | 4 | 7 | Resize VM action | Job id, new size | — | No |
| 3.2.2 | Configuration update through Ansible | 0 | 8 | Ansible day-2 action | Job output | [Ansible](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/ansible) | Yes, weight 0 |
| 3.2.3 | Lifecycle actions | 3 | 3 | `run_server_action` stop, start, reboot | Job ids | [Lifecycle actions](https://academy.university.cloudbolt.io/guides/019403d1-422e-7471-826c-4a6c12820e2a/tasks/0193e483-6d74-7c94-a8c4-464683bd3b51) | No |

## Extensibility (weights total 42)

| Id | Item | Weight | Phase | How | Evidence | Docs | Skippable |
|---|---|---|---|---|---|---|---|
| 4.1.1 | No in-guest agents | 5 | — | Architecture statement; the workbook pre-scores it | — | — | No |
| 4.1.2 | Agentless in-guest script | 5 | 8 | Ad-Hoc Script server action | Script output | — | No |
| 4.2.1 | UI extension from the Content Library | 5 | 8 | Jupyter XUI install (UI) | Extension launches | [UI extensions](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/ui-extensions) | Yes, when P5 unmet |
| 4.2.2 | Recurring job on a schedule | 4 | 8 | Cleanup tmp job, cron, run | Job id, file gone | [Recurring jobs](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/recurring-jobs) | Yes, when P5 unmet |
| 4.2.3 | Rule-based automation | 3 | 8 | Quota rule enabled, executed | Email received | [Rules](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/rules) | Partial without P7 |
| 4.2.4 | Continuous infrastructure testing | 3 | 8 | CIT from a successful order | Test run | [CIT](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/continuous-infrastructure-testing) | No |
| 4.2.5 | Inbound webhook | 4 | 8 | Echo webhook, curl POST | Response JSON | [Inbound webhooks](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/inbound-webhooks/) | No |
| 4.2.6 | Customizable notification templates | 0 | 8 | Edit Order Complete template | Email body | [Email templates](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/customizing-email-templates) | Yes, without P7 |
| 4.3.1 | Python generated options | 5 | 4 | Generated-options action on a parameter | Dropdown contents | [Generated options](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/options-generated-using-orchestration-actions) | No |
| 4.3.2 | PowerShell build item | 4 | 8 | Hello World on a Windows blueprint | Job output | [Remote scripts](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/remote-scripts) | Yes, when no Windows image |
| 4.3.3 | Bash build item | 4 | 8 | Hello World on a Linux blueprint | Job output | [Remote scripts](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/remote-scripts) | Yes, when no Linux image |
| 4.3.4 | Terraform-backed blueprint | 0 | 8 | Terraform blueprint | Plan and apply logs | [Terraform](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/terraform-integration-through-cmp-blueprints) | Yes, weight 0 |
| 4.3.5 | Ansible Automation Platform | 0 | 8 | AAP configuration manager | Job template run | [AAP](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/ansible-automation-platform) | Yes, weight 0 |

Possible points per category are five times the weight total: 175, 195, 130, 210, for 710 in all.
