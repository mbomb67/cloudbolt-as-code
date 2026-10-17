# Suggest Domain OUs

Generated Parameter Options action that turns the `domain_ou` parameter into a type-ahead. After three characters it lists the Active Directory OUs whose distinguished name or path contains the text, ignoring case, so a parent OU or the domain name finds the OUs under it. Choices are labeled with their path (`corp.example.com/Servers/Web`), and those whose own name matches come first. The saved value is the OU's distinguished name, from which the join action takes the domain and its LDAP Utility.

| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-0zcrvpwu | Suggest Domain OUs (Generated Parameter Options) |
| Plugin | OHK-z9qrccyu | Suggest Domain OUs |
| Shared module | SHM-gcba1u31 | ad_domain |

Part of [PKG-nf407lm5](../../packages/PKG-nf407lm5/) with [Join Windows Server to AD Domain](../HPA-jjp1vlfi/).

## Setup
1. Sync the package, whose `domain_ou` parameter ([parameters/CF-8pif9mbf](../../parameters/CF-8pif9mbf/)) brings this action and links it, or sync the action alone: the plugin's metadata lists `domain_ou` under `parameters`, which links it too (an appliance export drops that list, so restore it after one). Admin > Parameters > Domain OU then lists the action under the options.
2. Add `domain_ou` to the environment, group, or blueprint where users order.
3. The LDAP Utility's account must be able to read the domain's OUs, and the appliance must reach a domain controller on the utility's port.

## Notes
- Every LDAP Utility's domain is searched: CloudBolt passes a type-ahead only the typed text, so another parameter cannot narrow it.
- Each LDAP Utility's OU list is read with one paged LDAP search under the domain its server reports (`defaultNamingContext`), not the utility's Domain field, and cached for 5 minutes, so a new OU can take that long to appear.
- At most 25 matches are shown; type more to narrow the list.
- A domain whose LDAP search fails or finds no OUs adds a choice that says why, shown when nothing matched or when you type that domain's name.
- A blueprint whose metadata lists `domain_ou` under `parameters` without this action in `gen_options_hooks` unlinks it when that blueprint syncs. Sync this action again afterwards.
