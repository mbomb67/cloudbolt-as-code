# Join Windows Server to AD Domain

Post-Provision orchestration action that joins each Windows server in the job to Active Directory by running `Add-Computer` in the guest, in the OU its `domain_ou` parameter names. The domain is the OU's DC components, and the join account is the service account of the LDAP Utility that serves that domain. After the join the action waits for the restart, checks the secure channel, and records the computer object's GUID and DN on the server (`ad_computer_object_guid`, `ad_computer_object_dn`).

`domain_to_join` is optional. Without an OU it names the domain to join, into the default Computers container. It also chooses between several LDAP Utilities for one domain, and when set it must match the OU's domain. Servers with neither parameter are skipped.

| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-jjp1vlfi | Join Windows Server to AD Domain (Post-Provision) |
| Plugin | OHK-9oqn6pur | Join Windows Server to AD Domain |
| Shared module | SHM-gcba1u31 | ad_domain |

Related: [Suggest Domain OUs](../HPA-0zcrvpwu/) turns `domain_ou` into a type-ahead OU picker, and [Delete Windows Computer Object from AD](../HPA-q181uofy/) removes the object at decommission. Content package [PKG-nf407lm5](../../packages/PKG-nf407lm5/) syncs all three. Linux servers: [HPA-o6ctckmt](../HPA-o6ctckmt/).

## Prerequisites
- An LDAP Utility (Admin > LDAP Authentication) per domain: Account in `user@domain` form, its password, and the address of a domain controller the appliance can reach (the OU picker, the GUID lookup and the delete use LDAP from the appliance). The domain is read from that server (its `defaultNamingContext`), so the utility's Domain field may be a sign-in domain that differs from the AD domain.
- The utility's account is the join account. Delegate on the target OUs: Create Computer objects, and on computer objects Reset password, Read and write account restrictions, Validated write to DNS host name, and Validated write to service principal name. Not Domain Admin.
- `domain_ou` on the environment, group, or blueprint where users order.
- Guest: Windows Server with Windows PowerShell 5.1, DNS servers that resolve the domain's domain controller SRV records, and access to the domain controllers on 53, 88, 135, 389, 445, 464 and 49152-65535.
- CloudBolt must reach the guest: the handler's own channel (VMware Tools, Azure Run Command, AWS SSM) when the server's `tech_specific_script_execution` is on, otherwise WinRM with the server's credentials.

## Notes
- vCenter: CloudBolt's own guest customization joins the domain while the VM is built only when `domain_to_join` is set. This action then finds it joined, skips the join and restart, and records the GUID. With `domain_ou` alone, this action does the join.
- A new VM can pass its cloud's reachability check before it takes scripts (the SSM agent registers, WinRM starts), so the action retries for up to 10 minutes while CloudBolt cannot run a script in the guest. On AWS with tech-specific execution, the instance needs an IAM instance profile that allows Systems Manager (`AmazonSSMManagedInstanceCore`).
- The script schedules a restart 15 seconds after `Add-Computer`; the action waits up to 20 minutes for the guest to come back in the domain.
- Exit codes in the job log: 3 the domain controller SRV record does not resolve (DNS), 4 `Add-Computer` failed (rights, password, domain controller access, or an existing computer object the join account did not create, which Windows refuses to reuse since KB5020276), 5 the restart could not be scheduled, 7 already in another domain.
- The join password travels in the script body, base64 encoded and decoded into a SecureString, and is scrubbed from the job output. Run Command and WinRM keep no copy; AWS Systems Manager keeps the command in its history, so limit `ssm:ListCommands` on those accounts.
- Imports enabled with `continue_on_failure: false`, so a failed join fails the order.
