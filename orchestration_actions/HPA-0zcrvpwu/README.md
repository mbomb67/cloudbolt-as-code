# Suggest Domain OUs

Generated Parameter Options action that turns the `domain_ou` parameter into a type-ahead. After three characters it lists the Active Directory OUs whose name contains the text, labeled with their path (`corp.example.com/Servers/Web`). The saved value is the OU's distinguished name, which the Windows and Linux join actions and CloudBolt's vCenter guest customization all read.

| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-0zcrvpwu | Suggest Domain OUs (Generated Parameter Options) |
| Plugin | OHK-z9qrccyu | Suggest Domain OUs |
| Shared module | SHM-gcba1u31 | ad_domain |

Part of [PKG-nf407lm5](../../packages/PKG-nf407lm5/) with [Join Windows Server to AD Domain](../HPA-jjp1vlfi/).

## Setup
1. Sync the action. The plugin's metadata links it to `domain_ou`, so no Parameters page step is needed.
2. Add `domain_ou` next to `domain_to_join` on the environment, group, or blueprint where users order.
3. The LDAP Utility's account must be able to read the domain's OUs, and the appliance must reach a domain controller on the utility's port.

## Notes
- The list is not narrowed by `domain_to_join`: CloudBolt calls the action with only the parameter and the typed text. With one LDAP Utility this makes no difference. With several, every domain is searched and each label starts with its domain, and the join action rejects an OU outside the server's domain.
- At most 25 matches are shown; type more to narrow the list.
- A blueprint whose metadata lists `domain_ou` under `parameters` without this action in `gen_options_hooks` unlinks it when that blueprint syncs. Sync this action again afterwards.
