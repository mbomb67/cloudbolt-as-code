# POC Server

Deploys one server into any environment the ordering group may use, runs a Hello World script inside the guest (Bash on Linux, PowerShell on Windows; the script that does not match the OS family is skipped), and asks for a POC Application Tier whose options a Python action generates. It is the Custom Server blueprint plus the three things the POC Test Plan asks for in extensibility: post-provisioning automation (1.1.3), in-guest scripts in PowerShell and Bash (4.3.2, 4.3.3) and Python-generated parameter options (4.3.1).

## Contents
| Role | ID | Name |
|---|---|---|
| Blueprint | BP-x78p48gz | POC Server |
| Remote Script (Linux) | [OHK-818zw61m](../../plugins/OHK-818zw61m/) | POC Hello World (Linux) |
| Remote Script (Windows) | [OHK-u7j8o0vm](../../plugins/OHK-u7j8o0vm/) | POC Hello World (Windows) |
| Generated options action | [HPA-2ud31gkx](../../orchestration_actions/HPA-2ud31gkx/README.md) | Generate options for 'POC Application Tier' |

## Prerequisites
- An environment with an OS build whose image has working guest credentials: CloudBolt reaches Linux over SSH (port 22) and Windows over WinRM (5985 or 5986) from the appliance. Set the credentials on the OS build or the server.
- The ordering group may deploy into the environment. The blueprint is open to every group.

## Setup
1. Sync `blueprints/BP-x78p48gz` (the scripts and the generated-options action come with it); the POC onboarding skill does this in its extensibility phase with `sync_from_source_control_repo`.
2. Order it: pick the environment and OS build, choose a POC Application Tier, submit. The job log of the Hello World item shows the message printed inside the guest.
3. The server carries `poc_application_tier` as an attribute; map it to a cloud tag with `onb_tag_mapping` to show tagging from a parameter (2.5.1).
