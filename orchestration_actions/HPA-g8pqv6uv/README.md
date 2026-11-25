# Generate Options for Domain Service Account

Generated Parameter Options action that lists every connection info labeled `domain_service_account` as the choices for the `domain_service_account` parameter, shown as name and username. The saved value is the connection info's ID. [Join Windows Server to AD Domain](../HPA-jjp1vlfi/) makes that account the server's CloudBolt credentials after it joins the server to its `domain_ou`. The empty choice keeps the local credentials.

| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-g8pqv6uv | Generate Options for Domain Service Account (Generated Parameter Options) |
| Plugin | OHK-ww524a3e | Generate Options for Domain Service Account |

Part of [PKG-nf407lm5](../../packages/PKG-nf407lm5/). Setup of the connection infos: [Join Windows Server to AD Domain › Domain service account](../HPA-jjp1vlfi/#domain-service-account-optional).

## Notes
- Sync the package, whose `domain_service_account` parameter ([parameters/CF-yqmreb5m](../../parameters/CF-yqmreb5m/)) brings this action and links it, or sync the action alone: the plugin's metadata lists `domain_service_account` under `parameters`, which links it too (an appliance export drops that list, so restore it after one).
- The list does not check the connection infos. The join action fails a server, before joining it, when the picked one has no password or its username has no domain.
