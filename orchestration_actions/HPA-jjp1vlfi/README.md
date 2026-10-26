# Join Windows Server to AD Domain

Post-Provision orchestration action that joins each Windows server in the job to Active Directory by running `Add-Computer` in the guest, in the OU its `domain_ou` parameter names. The domain is the OU's DC components, and the join account is the service account of the LDAP Utility that serves that domain. The computer joins under the CloudBolt hostname: when the guest has another name (AWS Windows AMIs boot as `EC2AMAZ-...`), `Add-Computer -NewName` renames it in the same restart, and a computer already in the domain under another name is renamed with `Rename-Computer`. After the join the action waits for the restart, checks the computer name and the secure channel, and records the computer object's GUID and DN on the server (`ad_computer_object_guid`, `ad_computer_object_dn`).

Servers without `domain_ou` are skipped. `domain_to_join` is not read: CloudBolt's own guest customization joins servers that have it.

When `domain_service_account` is set and this action joins the server, the action switches the server's CloudBolt credentials to that connection info's username and password as soon as the restart is scheduled. The check after the restart runs as that account, and so does every later remote script. A server that was already in the domain keeps its credentials.

| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-jjp1vlfi | Join Windows Server to AD Domain (Post-Provision) |
| Plugin | OHK-9oqn6pur | Join Windows Server to AD Domain |
| Shared module | SHM-gcba1u31 | ad_domain |

Related: [Suggest Domain OUs](../HPA-0zcrvpwu/) turns `domain_ou` into a type-ahead OU picker, [Generate Options for Domain Service Account](../HPA-g8pqv6uv/) lists the service accounts, and [Delete Windows Computer Object from AD](../HPA-q181uofy/) removes the object at decommission. Content package [PKG-nf407lm5](../../packages/PKG-nf407lm5/) syncs them all. Linux servers: [HPA-o6ctckmt](../HPA-o6ctckmt/).

## Prerequisites
- One LDAP Utility (Admin > LDAP Authentication) per domain: Account in `user@domain` form, its password, and the address of a domain controller the appliance can reach (the OU picker, the GUID lookup and the delete use LDAP from the appliance). The domain is read from that server (its `defaultNamingContext`), so the utility's Domain field may be a sign-in domain that differs from the AD domain. With two utilities for one domain the join fails; keep one.
- The utility's account is the join account. Delegate on the target OUs: Create Computer objects, and on computer objects Reset password, Read and write account restrictions, Validated write to DNS host name, and Validated write to service principal name. Not Domain Admin.
- `domain_ou` on the environment, group, or blueprint where users order.
- Hostnames that are valid Windows computer names: at most 15 letters, digits, and hyphens, not only digits. The action fails a server whose hostname's first label is not, rather than join it under a truncated name.
- Guest: Windows Server with Windows PowerShell 5.1, DNS servers that resolve the domain's domain controller SRV records, and access to the domain controllers on 53, 88, 135, 389, 445, 464 and 49152-65535.
- CloudBolt must reach the guest: the handler's own channel (VMware Tools, Azure Run Command, AWS SSM) when the server's `tech_specific_script_execution` is on, otherwise WinRM with the server's credentials.

## Domain service account (optional)
1. Admin > Connection Info > Add:
   - Name: what users pick, e.g. `CORP server admin`.
   - IP/Hostname: the domain name, e.g. `corp.example.com`. CloudBolt requires the field; the action does not read it.
   - Username: the account with its domain, in UPN form: `svc-cloudbolt@corp.example.com`. `CORP\svc-cloudbolt` also works. A username without a domain fails the server before the join.
   - Password: the account's password.
   - Labels: `domain_service_account`.
2. Add `domain_service_account` next to `domain_ou` on the environment, group, or blueprint. Its dropdown lists every connection info with that label; the empty choice keeps the local credentials.
3. Make the account a member of the local Administrators group of the servers it signs in to, for example with a Group Policy (Restricted Groups) linked to their OUs. Membership must apply at the restart after the join, because the check after it signs in as this account.

The switch writes the server's `username` and `password` parameters, the same ones Edit CB Server Credentials sets. Only WinRM and VMware Tools sign in with them; AWS SSM and Azure Run Command ignore them. WinRM signs in with NTLM unless the server's `winrm_transport` parameter names another transport. If the server does not answer as the account within the 20-minute wait, the job fails and the server keeps the domain account's credentials.

## Notes
- vCenter: CloudBolt's own guest customization joins the domain while the VM is built only when `domain_to_join` is set. With `domain_ou` too, this action then finds it joined, skips the join and restart, keeps the server's credentials, and records the GUID.
- A new VM can pass its cloud's reachability check before it takes scripts (the SSM agent registers, WinRM starts), so the action retries for up to 10 minutes while CloudBolt cannot run a script in the guest. On AWS with tech-specific execution, the instance needs an IAM instance profile that allows Systems Manager (`AmazonSSMManagedInstanceCore`).
- The script schedules a restart 15 seconds after `Add-Computer`; the action waits up to 20 minutes for the guest to come back in the domain.
- Exit codes in the job log: 3 the domain controller SRV record does not resolve (DNS), 4 `Add-Computer` failed (rights, password, domain controller access, or an existing computer object the join account did not create, which Windows refuses to reuse since KB5020276), 5 the restart could not be scheduled, 7 already in another domain, 9 the name after the restart is not the CloudBolt hostname, 10 renaming a computer already in the domain failed (the join account needs rights to rename computer objects).
- The join password travels in the script body, base64 encoded and decoded into a SecureString, and is scrubbed from the job output. Run Command and WinRM keep no copy; AWS Systems Manager keeps the command in its history, so limit `ssm:ListCommands` on those accounts. The service account's password goes only into the server's encrypted `password` parameter.
- Only connection infos labeled `domain_service_account` are accepted, so an order cannot put another connection info's credentials on its server.
- Imports enabled with `continue_on_failure: false`, so a failed join fails the order.
