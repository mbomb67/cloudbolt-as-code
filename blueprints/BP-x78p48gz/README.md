# Standard Server

Deploys one server into any environment the ordering group may use, runs a Hello World script inside the guest (Bash on Linux, PowerShell on Windows; the script that does not match the OS family is skipped), and asks for the application and maintenance window, whose options Python actions generate. It covers the test plan's post-provisioning automation (1.1.3), in-guest scripts in PowerShell and Bash (4.3.2, 4.3.3) and Python-generated parameter options (4.3.1).

## Contents
| Role | ID | Name |
|---|---|---|
| Blueprint | BP-x78p48gz | Standard Server |
| Remote Script (Linux) | [OHK-818zw61m](../../plugins/OHK-818zw61m/) | Hello World (Linux) |
| Remote Script (Windows) | [OHK-u7j8o0vm](../../plugins/OHK-u7j8o0vm/) | Hello World (Windows) |
| Generated options action | [HPA-2ud31gkx](../../orchestration_actions/HPA-2ud31gkx/README.md) | Generate options for 'Application' |
| Generated options action | [HPA-onelxnkw](../../orchestration_actions/HPA-onelxnkw/README.md) | Generate options for 'Maintenance Window' |

## Prerequisites
- An environment with an OS build whose image has working guest credentials: CloudBolt reaches Linux over SSH (port 22) and Windows over WinRM (5985 or 5986) from the appliance. Set the credentials on the OS build or the server.
- The ordering group may deploy into the environment. The blueprint is open to every group.
- No other parameter named `application` or `maintenance_window` on the appliance, or agreement that the blueprint uses it: a sync reuses an existing parameter with the same name. `onb_status` lists such parameters under `parameterConflicts`.

## Setup
1. Sync `blueprints/BP-x78p48gz` (the scripts and both generated-options actions come with it); the onboarding skill does this in its extensibility phase with `sync_from_source_control_repo`, after checking `parameterConflicts`.
2. Order it: pick the environment and OS build, an application and a maintenance window, submit. The job log of the Hello World item shows the message printed inside the guest.
3. The server carries `application` and `maintenance_window` as attributes; map them to cloud tags with `onb_tag_mapping` (`application=Application`, `maintenance_window=MaintenanceWindow`).
