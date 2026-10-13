# Migrate Aria Automation Projects to Groups

Creates a CloudBolt group for each Aria Automation project you select, maps the project's Active Directory group role assignments to CloudBolt roles through LDAP mappings, and copies the project's unencrypted custom properties onto the group as parameters. Safe to re-run: groups, mappings and parameters are get-or-create.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-gyl8wlrq | Migrate Aria Automation Projects |
| Shared module | SHM-u2s2d40p | aria_connection |

## Prerequisites
- A ConnectionInfo for the Aria Automation appliance with the label `vra8` or `aria`: protocol https, the appliance FQDN, port 443, a username in `user@domain` form (leave the domain off for the Aria System Domain) and its password. The account needs read access to the projects being migrated.
- A CloudBolt LDAP utility for every Active Directory domain that appears in the projects' group assignments. The Aria identity-provider domain name must match the LDAP utility's domain, or be mapped to it in `DOMAIN_MAP` at the top of the plugin.
- If the appliance certificate is not publicly trusted, add it under Admin > SSL Certificates.

## Setup
1. Create and label the ConnectionInfo.
2. Order the blueprint: pick the connection and the projects. The optional Groups Map is a JSON object of Aria project name to CloudBolt group name for projects that should land in a differently named or shared group.

## Notes
- Role mapping is fixed in the plugin's `ROLE_MAP`: administrators to group_admin, members to requestor, viewers to viewer, supervisor to approver.
- Only Active Directory group assignments migrate. Individual user assignments are logged and skipped.
- Encrypted custom properties (values starting with `((secret:`) are skipped.
