# Delete Windows Computer Object from AD

Pre-Delete orchestration action that deletes each Windows server's Active Directory computer object over LDAP, bound as the LDAP Utility for its domain (taken from the recorded object DN, `domain_ou`, or `domain_to_join`, so servers CloudBolt's guest customization joined are covered too). It deletes the object whose GUID [Join Windows Server to AD Domain](../HPA-jjp1vlfi/) recorded. Without a GUID it deletes the object named after the computer, but only when exactly one matches and AD created it after CloudBolt added the server. Child objects, such as BitLocker recovery information, are deleted with it. The guest is not contacted.

It also deletes the DNS records the server registered in AD-integrated zones: its host name and the reverse (PTR) record of each of that name's addresses. A record is deleted only when the computer object owns it, as with every record a server registers through secure dynamic update. Records an administrator created are left alone. Without this, the zone keeps the record. Only its owner can update it, so the next server with the same name cannot register its own. Records are marked deleted the way the DNS server marks them, and DNS servers stop answering for the name at their next directory poll (3 minutes by default).

| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-q181uofy | Delete Windows Computer Object from AD (Pre-Delete) |
| Plugin | OHK-bkoau0j8 | Delete Windows Computer Object from AD |
| Shared module | SHM-gcba1u31 | ad_domain |

Part of [PKG-nf407lm5](../../packages/PKG-nf407lm5/).

## Prerequisites
- The LDAP Utility's account needs Delete Computer objects on the OUs, plus Delete subtree for objects with children.
- For DNS records, it needs Write on the records in the DNS zones. Members of DnsAdmins and Domain Admins have that by default. Without it the computer object is still deleted and the job reports the DNS failure.
- The appliance must reach a domain controller on the utility's port.

## Notes
- Imports with `continue_on_failure: true`: an AD error shows in the job without stopping the server's deletion.
- DNS records are found only from a computer object it deletes. A server whose object is already gone keeps its records.
- A server registers its name again whenever its addresses change, and it keeps working credentials until it is off. One whose addresses change while it shuts down, such as a failover cluster node whose cluster is still running, gets its record back after this action. Turn off its DNS registration or destroy the cluster first, as the SCVMM lab's teardown does.
- CloudBolt's out-of-the-box "Delete Server from LDAP" action matches on name only. Leave it disabled when this action is enabled.
