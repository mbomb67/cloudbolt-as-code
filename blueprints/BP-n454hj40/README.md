# Create VPC

Creates an AWS VPC by running an Ansible playbook through a CloudBolt Ansible Configuration Manager, then creates a CloudBolt environment on the chosen AWS resource handler pinned to the new VPC and region. The orderer supplies only a VPC name and an AWS region. Teardown runs a second playbook that receives the stored VPC ID.

## Contents
| Role | ID | Name |
|---|---|---|
| Build (seq 1) | OHK-50ntkska | Set VPC Name |
| Build (seq 2) | OHK-xdjd5o9b | Ansible Ad-Hoc |
| Build (seq 3) | OHK-yd1af0eu | Create Environment |
| Teardown | OHK-xdjd5o9b | Ansible Ad-Hoc |

## Prerequisites
- An Ansible Configuration Manager in CloudBolt whose ConnectionInfo can run scripts on the Ansible control host. The playbook is executed on that host with `ansible-playbook`, so `ansible` and the AWS collections it uses must be installed there, with AWS credentials available to it.
- Two playbooks present on the control host (they are not shipped in this repo): a create playbook that reads `vpc_name` and `aws_region` as extra vars and reports the new VPC ID with `set_stats` (key `vpc_id`), and a teardown playbook that reads `vpc_id`.
- An AWS resource handler for the Create Environment step.

## Setup
1. In `BP-n454hj40_metadata.json`, replace the `FILL-ME` value of `ansible_manager_a332` (on both the build and teardown Ansible Ad-Hoc items) with the ID of your Ansible Configuration Manager. The plugin fails with a clear message while the placeholder is in place.
2. Set `playbook_path_a332` on both items to the playbook paths on your control host (ships `/home/ec2-user/create-vpc-from-vars.yml` and `/home/ec2-user/teardown-vpc.yml`).
3. Replace the `FILL-ME` value of `aws_handler_a336` on the Create Environment item with the ID of your AWS resource handler.
4. In `plugins/OHK-yd1af0eu/OHK-yd1af0eu_script.py`, replace the placeholder values in `CFV_OPTIONS` (`your-keypair-name`, `your-security-group`) if you extend the plugin to seed environment options; the shipped `run()` does not read them.
5. Sync the repo. The Ansible Configuration Manager and AWS handler are not part of this repo; configure them on the instance.

## Notes
- Ansible Ad-Hoc passes every custom field on the resource to the playbook as extra vars, stripping the `ansible_` prefix (`ansible_vpc_name` becomes `vpc_name`). Values the playbook publishes with `set_stats` are stored back on the resource as `ansible_<key>`; Create Environment requires `ansible_vpc_id`, `ansible_aws_region`, and `ansible_vpc_name` to be present and fails otherwise.
- The AWS Region field offers `us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`; edit the blueprint parameter's `global_options` to change the list.
- The playbook runs with a 600-second timeout via the Configuration Manager's ConnectionInfo.
- Teardown deletes the VPC through the playbook but does not remove the CloudBolt environment created at build time.
