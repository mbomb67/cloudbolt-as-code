# Migrate Aria Automation Deployments to Resources

For one Aria Automation cloud template, imports every successful deployment into CloudBolt under the blueprint you choose: a resource per deployment with its servers attached, or stand-alone servers when the chosen blueprint has no resource type. VMs are matched to servers CloudBolt already synced from vCenter by instance UUID, then given the deployment's owner, the project's group, the environment of their vSphere cluster, and every custom property as a parameter. Safe to re-run: resources are keyed by the `vra_id` parameter.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-7sq2hqrb | Migrate Aria Automation Deployments |
| Form | FRM-i53vgmce | Migrate Aria Automation Deployments to Resources |
| Webhook | IWH-xr0508nf | Aria Migration Form Options (plugin OHK-xhpq1rn3) |
| Shared module | SHM-u2s2d40p | aria_connection |

## Prerequisites
- A ConnectionInfo for the Aria Automation appliance with the label `vra8` or `aria`: protocol https, the appliance FQDN, port 443, a username in `user@domain` form (leave the domain off for the Aria System Domain) and its password.
- The VMs already exist in CloudBolt: a vCenter resource handler that has synced them, and an environment per vSphere cluster so each server lands in the right one.
- A target CloudBolt blueprint with one server tier or sub-blueprint tier per resource on the Aria canvas. The order form maps canvas resource names to tiers.
- The groups exist, either from the Projects migration blueprint or through the form's Groups Map, and a CloudBolt LDAP utility covers the owners' domain so owners can be created.
- If the appliance certificate is not publicly trusted, add it under Admin > SSL Certificates.

## Setup
1. Create and label the ConnectionInfo.
2. Sync `webhooks/` as well as this blueprint. A blueprint sync does not refresh the webhook its form calls.
3. Order the blueprint: pick the connection, the cloud template and optionally the projects to limit it to; then the CloudBolt blueprint, the resource-to-tier map, and any parameter prefix or ignored prefixes.

## Notes
- Only `Cloud.vSphere.Machine` resources become servers. Other resource types are skipped with a warning.
- If you supply a Groups Map, include every project in it. Deployments for projects missing from the map are skipped.
- Custom property values longer than 400 characters become text parameters, and a property named `Application` becomes `Application_`.
- OneFuse properties are skipped unless you enable them on the last page of the form. Use the endpoint map only when the OneFuse endpoint names differ between Aria and CloudBolt.
