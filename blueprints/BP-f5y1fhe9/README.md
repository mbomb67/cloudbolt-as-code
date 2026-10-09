# Run Aria Orchestrator Workflow

Runs an Aria Orchestrator workflow as the build step of a CloudBolt resource, waits for it to finish, and writes its outputs onto the resource as parameters. On delete the same plugin runs the workflow again with teardown parameters rebuilt from the stored outputs. Use it to keep existing Orchestrator workflows in service from the CloudBolt catalog while they are migrated.

## Contents
| Role | ID | Name |
|---|---|---|
| Build and teardown | OHK-3bgpjmlr | Run Aria Orchestrator Workflow |
| Shared module | SHM-u2s2d40p | aria_connection |

## Prerequisites
- A ConnectionInfo for the Aria Automation appliance with the label `vra8` or `aria`: protocol https, the appliance FQDN, port 443, a username in `user@domain` form (leave the domain off for the Aria System Domain) and its password. The account needs rights to run the workflow. The Orchestrator API is reached at `/vco/api` on the same host, so an external Orchestrator is not supported.
- If the appliance certificate is not publicly trusted, add it under Admin > SSL Certificates.

## Setup
1. Create and label the ConnectionInfo.
2. Order the blueprint: pick the connection and the workflow, enter the Workflow Parameters as a JSON object of input name to value, and choose whether sdk-object outputs are flattened into one parameter per attribute.
3. For a teardown that must hand an sdk-object back to the workflow, add a `teardown_sdk_<name>` entry to the teardown item's Workflow Parameters default. The plugin docstring shows the shape.

## Notes
- Parameter values are rendered through Django templates, so they can reference the resource, its group and the job.
- Supported input types are string, number, boolean, Properties and arrays of those. sdk-object inputs are only supported through the teardown mechanism.
- Outputs are stored as parameters named `vro_<workflow id>_<output>`.
- The plugin waits up to 300 seconds for the workflow. A longer workflow fails the job while the Orchestrator run continues.
