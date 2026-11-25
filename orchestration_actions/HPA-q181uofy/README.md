# Delete Windows Computer Object from AD

Pre-Delete orchestration action that deletes each Windows server's Active Directory computer object over LDAP, bound as the LDAP Utility for its domain (taken from the recorded object DN, `domain_ou`, or `domain_to_join`, so servers CloudBolt's guest customization joined are covered too). It deletes the object whose GUID [Join Windows Server to AD Domain](../HPA-jjp1vlfi/) recorded. Without a GUID it deletes the object named after the computer, but only when exactly one matches and AD created it after CloudBolt added the server. Child objects, such as BitLocker recovery information, are deleted with it. The guest is not contacted.

| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-q181uofy | Delete Windows Computer Object from AD (Pre-Delete) |
| Plugin | OHK-bkoau0j8 | Delete Windows Computer Object from AD |
| Shared module | SHM-gcba1u31 | ad_domain |

Part of [PKG-nf407lm5](../../packages/PKG-nf407lm5/).

## Prerequisites
- The LDAP Utility's account needs Delete Computer objects on the OUs, plus Delete subtree for objects with children.
- The appliance must reach a domain controller on the utility's port.

## Notes
- Imports with `continue_on_failure: true`: an AD error shows in the job without stopping the server's deletion.
- The A record the server registered in AD DNS is not deleted with the computer object. DNS scavenging removes it once it ages out, if scavenging is on for the zone.
- CloudBolt's out-of-the-box "Delete Server from LDAP" action matches on name only. Leave it disabled when this action is enabled.
