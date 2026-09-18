# Orchestration actions

Lifecycle hooks. Actions marked disabled ship that way on purpose; read the README before enabling.

| Action | ID | Hook point | Enabled | Plugin |
|---|---|---|---|---|
| AD DNS - Create A Record | [HPA-dnscrt01](HPA-dnscrt01/README.md) | Post-Provision | no | `OHK-dnsadd01` |
| AD DNS - Delete A Record | [HPA-dnsdec01](HPA-dnsdec01/README.md) | Pre-Delete | no | `OHK-dnsdel01` |
| Azure CMK - Per-VM Disk Encryption Set | [HPA-w1dmx20b](HPA-w1dmx20b/README.md) | Post-Provision | no | `OHK-vklpnqhq` |
| Azure CMK - Remove Per-VM Disk Encryption Set | [HPA-h7g0i0dx](HPA-h7g0i0dx/README.md) | Post-Delete | no | `OHK-2vpg4pff` |
| Azure NSG - Attach VM to NSG | [HPA-vo2ghe4x](HPA-vo2ghe4x/README.md) | Post-Provision | no | `OHK-t8fjx0kz` |
| Azure Resource Manager Rate Hook | [HPA-t7hlyvyy](HPA-t7hlyvyy/README.md) | Compute Server Rate | yes | `OHK-vg0rmi7i` |
| Generate options for 'Expiration Date' | [HPA-qb0w86mi](HPA-qb0w86mi/README.md) | Generated Parameter Options | yes | `OHK-cfciy0fo` |
| Join Linux Server to AD Domain | [HPA-o6ctckmt](HPA-o6ctckmt/README.md) | Post-Provision | yes | `OHK-exqmrl0e` |
