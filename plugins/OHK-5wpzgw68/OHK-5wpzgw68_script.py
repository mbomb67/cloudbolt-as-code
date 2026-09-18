from common.methods import set_progress
from resourcehandlers.aws.models import AWSHandler
from utilities.exceptions import CommandExecutionException
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

def generate_options_for_aws_rh_id(field, server=None, **kwargs):
    logger.debug(f"kwargs: {kwargs}")
    group = server.group
    available_environments = group.get_available_environments()
    rhs = [("", "------Select an AWS Account to Migrate To------")]
    for env in available_environments:
        logger.debug(f"kwargs env: {env}")
        if env.resource_handler:
            rh = env.resource_handler.cast()
            if rh.resource_technology.slug != "aws":
                continue
            rhs.append((rh.id, rh.name))
    return rhs


def generate_options_for_aws_region(field, server=None, **kwargs):
    options = [
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
    ]
    return options

def run(job, server=None, *args, **kwargs):
    rh = AWSHandler.objects.get(id="{{aws_rh_id}}")
    region = "{{aws_region}}"
    script = get_script(rh, region)
    server.execute_script(script_contents=script, timeout=1800)
    return "SUCCESS", "AWS Application Migration Service agent installation script executed.", ""


def get_script(rh, region="us-east-1"):
    return f"""#!/bin/bash
wget -O ./aws-replication-installer-init https://aws-application-migration-service-us-east-1.s3.us-east-1.amazonaws.com/latest/linux/aws-replication-installer-init

chmod +x aws-replication-installer-init;

./aws-replication-installer-init --region {region} --aws-access-key-id {rh.serviceaccount} --aws-secret-access-key {rh.servicepasswd} --no-prompt
"""