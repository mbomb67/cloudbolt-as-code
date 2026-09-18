"""
A CloudBolt Plugin used to Create an Environent tied to an AWSHandler, with the
AWS region and optional VPC ID stored as custom field values on the 
Environment. The plugin can be used in a workflow or run standalone. region and
optional VPC ID stored as custom field values on the Environment. The plugin 
can be used in a workflow or run standalone.
"""
from common.methods import set_progress
from c2_wrapper import create_custom_field
from infrastructure.models import Environment
from resourcehandlers.aws.models import AWSHandler
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

USE_DEFAULTS = ["aws_availability_zone"]

CFV_OPTIONS = {
    'key_name': ['your-keypair-name'],
    'delete_ebs_volumes_on_termination': [True],
    'ebs_volume_type': ['gp3'],
    'instance_type': ['t3.small', 't3.medium', 't3.large'],
    'sec_groups': ['your-security-group'],
 }

OS_BUILDS = ["Amazon Linux 2023", "Ubuntu 22.04"]

NETWORKS = [""]



def generate_options_for_aws_handler(field, **kwargs):
    handlers = AWSHandler.objects.all()
    return [(h.id, h.name) for h in handlers]


def run(job, resource=None, **kwargs):
    """

    """
    group = resource.group
    aws_handler_id = "{{aws_handler}}"
    if not aws_handler_id or "FILL-ME" in aws_handler_id:
        return "FAILURE", "", "aws_handler is not set. Edit the blueprint parameter_defaults (see README)."
    handler = AWSHandler.objects.get(id=int(aws_handler_id))

    region = resource.get_value_for_custom_field("{{region_field_name}}", None)
    vpc_id = resource.get_value_for_custom_field("{{vpc_id_field_name}}", None)
    env_name = resource.get_value_for_custom_field("{{env_name_field_name}}", 
               None)
    if not region:
        set_progress("Region is required to create an environment.")
        return "FAILURE", "Region is required", ""
    if not env_name:
        set_progress("Environment name is required to create an environment.")
        return "FAILURE", "Environment name is required", ""
    if not vpc_id:
        set_progress("VPC ID is required to create an environment.")
        return "FAILURE", "VPC ID is required", ""

    set_progress(f"Creating environment '{env_name}' in region '{region}' with"
                 f" VPC ID '{vpc_id}'")
    handler.create_location_specific_env(
        location_name=region,
        env_name=env_name,
        vpc_id=vpc_id,
    )

    """
    1. Keep fields with SET_DEFAULTS
    """

    msg = (f"Environment '{env_name}' created successfully in region '{region}'"
           f" with VPC ID '{vpc_id}'.")

    return "SUCCESS", msg, ""