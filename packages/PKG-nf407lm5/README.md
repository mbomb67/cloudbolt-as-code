# Windows AD Domain Join

Joins provisioned Windows servers to Active Directory in the OU picked from a type-ahead domain_ou parameter, taking the domain and its LDAP Utility from the OU, and deletes the computer object at decommission.

Content package `PKG-nf407lm5`, version 1.0.0. Sync it from the Content Packages admin page (Content Package Manager UI extension).

| Type | Name | Folder |
|---|---|---|
| Orchestration action | Delete Windows Computer Object from AD | [orchestration_actions/HPA-q181uofy](../../orchestration_actions/HPA-q181uofy/) |
| Orchestration action | Join Windows Server to AD Domain | [orchestration_actions/HPA-jjp1vlfi](../../orchestration_actions/HPA-jjp1vlfi/) |
| Orchestration action | Suggest Domain OUs | [orchestration_actions/HPA-0zcrvpwu](../../orchestration_actions/HPA-0zcrvpwu/) |
| Parameter | Domain OU | [parameters/CF-8pif9mbf](../../parameters/CF-8pif9mbf/) |
