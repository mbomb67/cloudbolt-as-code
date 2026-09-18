# Azure Cross-Tenant Subscription

Creates an Azure subscription in one tenant (for example dev) that bills to a Microsoft Customer Agreement (MCA) billing account in another tenant (for example production), places it in a management group and optionally adds a named Owner. Day-2 actions grant and revoke a least-privilege "CloudBolt Restricted Contributor" role, list subscription-scope access and install a public-exposure Azure Policy initiative.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-96zebx6i | Build Azure Subscription |
| Teardown | OHK-597w4hvq | Teardown Azure Subscription |
| Discovery | OHK-av52dzqm | Discover Azure Subscriptions |
| Day-2 action | RSA-1kpl3w0d | Grant Access (Grant Restricted Contributor Access, OHK-pr8q2szp) |
| Day-2 action | RSA-8zwsrcbv | Revoke Access (Revoke Restricted Contributor Access, OHK-myrfeogg) |
| Day-2 action | RSA-f7wb11ny | List Access (List Subscription Access, OHK-ovt0z46j) |
| Day-2 action | RSA-mgh3dn9p | Apply Policy (Apply Public-Exposure Policy, OHK-b2az2bn6) |
| Shared module | SHM-5hjzm9e4 | azure_subscription_helpers |

## Prerequisites
- One Azure (ARM) resource handler per tenant, each with its own service principal and at least one Environment the ordering group can use. The Source Tenant and Destination Tenant dropdowns list these handlers.
- Source-tenant service principal: read access to the MCA billing account and its billing profiles and invoice sections (`Microsoft.Billing`), and the right to create subscriptions under the chosen invoice section. Only billing accounts of type MicrosoftCustomerAgreement are offered.
- Destination-tenant service principal: Microsoft Graph `Directory.Read.All` (or `ServicePrincipalEndpoint.ReadWrite.All` plus `User.Read.All`) to resolve its own object ID and owner emails, and Owner or User Access Administrator on the new subscription for RBAC assignment.
- Day-2 actions run as the destination handler's service principal. Grant and Revoke need role-definition and role-assignment write (User Access Administrator or Owner at subscription scope); Apply Policy needs Resource Policy Contributor or Owner; List Access needs Graph `Group.Read.All` and `Application.Read.All` to resolve every principal name.
- Management Group, if used, must already exist in the destination tenant.

## Setup
1. Sync the repository; the blueprint pulls in the plugins, resource actions and shared module.
2. Confirm both handlers' service principals hold the roles above. Credentials live on the resource handlers, not in a ConnectionInfo, so nothing needs re-entering after sync.
3. Order the blueprint: Source Tenant, Billing Account, Invoice Section (its name should match the cost center used for AP routing), Destination Tenant, Management Group, Owner Email and Subscription Name.
4. After the first Grant on a subscription, run Apply Policy. Grant only recommends it in its output; it does not apply it.

## Notes
- Build resolves the destination SP's object ID via Graph and creates the subscription with that SP as owner, so ownership is accepted automatically with no email confirmation.
- Teardown cancels (soft-deletes) the subscription; Azure keeps it reactivatable for 90 days before permanent deletion. It is idempotent, returns WARNING when subscription metadata is missing and deletes a stuck Pending alias if the subscription was never created.
- Discovery enumerates subscriptions visible to every Azure handler and merges on `azure_subscription_id`. Discovered subscriptions lack `azure_subscription_source_rh_id`, so stuck-alias cleanup for them is manual; when several handlers see the same subscription, the lowest handler ID wins.
- Restricted Contributor is Contributor minus role-definition and role-assignment writes, resource-group and deployment deletes, and public-exposure operations (public IPs, NAT/Firewall/Bastion, Front Door, CDN, App Gateway and LB writes, public DNS zones, storage public access). Apply Policy backstops what the role cannot express (subnet exposure, storage publicNetworkAccess); it is one-shot per subscription, returns WARNING if already applied and FAILURE if a conflicting assignment exists.
- Revoke removes only the Restricted Contributor assignment and lists the other roles the user still holds. List Access shows direct subscription-scope assignments only; assignments inherited from management groups or the tenant root are excluded.
