# Deploy AWS App Migration Agent

Server action that installs the AWS Application Migration Service (MGN) replication agent on a Linux server. It runs a bash script on the target through CloudBolt's remote script execution: the script downloads `aws-replication-installer-init` from AWS and runs it non-interactively with the chosen region and the selected AWS resource handler's access key and secret.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-5wpzgw68 | Deploy AWS App Migration Agent |

## Prerequisites
- A Linux target server that CloudBolt can run scripts on (working remote-execution credentials), with `bash`, `wget`, and outbound HTTPS access to AWS.
- An AWS resource handler reachable from one of the server's group's environments. Its service account key must be allowed to register MGN agents, and MGN must be initialized in the target region.

## Setup
1. Sync the repo. No ConnectionInfo is involved; the script uses the AWS resource handler's stored access key and secret.
2. To offer regions other than `us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`, edit `generate_options_for_aws_region` in `plugins/OHK-5wpzgw68/OHK-5wpzgw68_script.py`.

## Notes
- The AWS account dropdown lists only AWS resource handlers attached to environments the server's group can use.
- The handler's access key and secret are passed as command-line arguments to the installer, so they are visible in the generated script and in the target's process list while it runs. Use a key scoped to MGN agent installation.
- The script runs with a 1800-second timeout and reports SUCCESS when the script finishes; check the job output for the installer's own result.
- The installer is always downloaded from the `us-east-1` AWS distribution bucket regardless of the selected region.
