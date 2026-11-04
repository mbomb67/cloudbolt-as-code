# AWS Security Groups

Orders, discovers, and deletes AWS VPC security groups and adds a Security Rules tab for viewing and editing their inbound and outbound rules.

Content package `PKG-wik86fh7`, version 1.0.0. Sync it from the Content Packages admin page (Content Package Manager UI extension).

| Type | Name | Folder |
|---|---|---|
| UI extension | AWS Security Group Management | [extensions/XUI-oj60n72c](../../extensions/XUI-oj60n72c/) |
| Blueprint | AWS Security Group | [blueprints/BP-ofwowhqd](../../blueprints/BP-ofwowhqd/) |

## After the first sync
Restart the web server (`systemctl restart httpd`): the UI extension adds URL paths.
