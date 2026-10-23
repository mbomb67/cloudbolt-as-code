"""
Creates an AWS security group in the VPC of the chosen Environment (the
region's default VPC when the Environment names none) and records it on the
resource for the teardown and discovery plugins and the Security Rules tab
(XUI-oj60n72c).

AWS is reached through the Environment's resource handler
(handler.get_boto3_client), which carries CloudBolt's credentials and proxy
settings. The handler is never shown on the order form.

AWS gives a new security group no inbound rules and one outbound rule that
allows all IPv4 traffic; rules are managed from the Security Rules tab.
  https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_CreateSecurityGroup.html
  https://docs.aws.amazon.com/vpc/latest/userguide/creating-security-groups.html
"""
import re

from botocore.exceptions import BotoCoreError, ClientError

from accounts.models import Group
from common.methods import set_progress
from infrastructure.models import CustomField
from infrastructure.models import Environment


# Names and descriptions: up to 255 letters, digits, spaces and
# ._-:/()#,@[]+=&;{}!$* ; names are unique per VPC (case-insensitive), cannot
# start with sg- and cannot be "default".
# https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_CreateSecurityGroup.html
# https://docs.aws.amazon.com/AWSEC2/latest/APIReference/errors-overview.html (InvalidGroup.Reserved)
ALLOWED_RE = re.compile(r"^[A-Za-z0-9 ._\-:/()#,@\[\]+=&;!$*{}]{1,255}$")


def _resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def generate_options_for_env_id(server=None, **kwargs):
    """RBAC-aware Environment selector, restricted to AWS-backed environments.

    Standard: Group.get_available_environments() returns the environments
    explicitly entitled to the requesting group (and its ancestors) PLUS any
    unconstrained environments (no groups assigned). Users must be able to
    order into both, so never filter Environment by group__in alone.
    See docs/agents/rbac-and-security.md.
    """
    group = _resolve_group(kwargs.get("group"))
    if not group:
        return []

    available_ids = [env.id for env in group.get_available_environments()]
    envs = Environment.objects.filter(
        id__in=available_ids,
        resource_handler__awshandler__isnull=False,
    ).order_by("name")
    if not envs.exists():
        return [("", "------ No AWS environments available ------")]

    return [(env.id, env.name) for env in envs]


def create_custom_fields_as_needed():
    CustomField.objects.get_or_create(
        name="aws_rh_id",
        type="STR",
        defaults={
            "label": "AWS RH ID",
            "description": "Used by the AWS blueprints",
        },
    )

    CustomField.objects.get_or_create(
        name="aws_region",
        type="STR",
        defaults={
            "label": "AWS Region",
            "description": "Used by the AWS blueprints",
        },
    )

    CustomField.objects.get_or_create(
        name="aws_security_group_name",
        type="STR",
        defaults={
            "label": "Security Group Name",
            "description": "Used by the AWS security group",
            "show_as_attribute": True,
        },
    )

    CustomField.objects.get_or_create(
        name="aws_security_group_id",
        type="STR",
        defaults={
            "label": "Security Group ID",
            "description": "Used by the AWS security group",
            "show_as_attribute": True,
        },
    )

    CustomField.objects.get_or_create(
        name="aws_vpc_id",
        type="STR",
        defaults={
            "label": "VPC ID",
            "description": "Used by the AWS security group",
            "show_as_attribute": True,
        },
    )


def _aws_message(exc):
    """AWS's error code and message, else the SDK's text."""
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return "{} ({})".format(error.get("Message", exc), error.get("Code", "ClientError"))
    return str(exc)


def _vpc_id(client, env, region):
    """The Environment's VPC, else the region's default VPC, else None.
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeVpcs.html
    """
    vpc_id = (env.get_cfv("vpc_id") or "").strip()
    if vpc_id:
        return vpc_id
    vpcs = client.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}]).get("Vpcs", [])
    return vpcs[0]["VpcId"] if vpcs else None


def run(job, **kwargs):
    create_custom_fields_as_needed()

    resource = kwargs.get("resource")
    env = Environment.objects.get(id="{{ env_id }}")
    name = "{{ security_group_name }}".strip()
    description = "{{ security_group_description }}".strip() or name

    if not ALLOWED_RE.match(name) or name.lower().startswith("sg-") or name.lower() == "default":
        return (
            "FAILURE",
            "",
            f"'{name}' is not a valid security group name: use up to 255 letters, digits, spaces and "
            "._-:/()#,@[]+=&;{}!$*, not starting with sg- and not 'default'.",
        )
    if not ALLOWED_RE.match(description):
        return (
            "FAILURE",
            "",
            "The description may use up to 255 letters, digits, spaces and ._-:/()#,@[]+=&;{}!$* only.",
        )

    rh = env.resource_handler.cast()
    region = env.get_cfv("aws_region")
    if not region:
        return "FAILURE", "", f"Environment '{env.name}' has no AWS region (aws_region)."

    set_progress(f"Connecting to AWS {region} through resource handler '{rh.name}'")
    try:
        client = rh.get_boto3_client(region, "ec2")
        vpc_id = _vpc_id(client, env, region)
    except (BotoCoreError, ClientError) as exc:
        return "FAILURE", "", f"Could not read the VPCs in {region}: {_aws_message(exc)}"
    if not vpc_id:
        return (
            "FAILURE",
            "",
            f"Environment '{env.name}' names no VPC (vpc_id) and {region} has no default VPC. "
            "Set the environment's VPC and order again.",
        )

    # https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeSecurityGroups.html
    try:
        existing = client.describe_security_groups(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "group-name", "Values": [name]},
            ]
        ).get("SecurityGroups", [])
    except (BotoCoreError, ClientError) as exc:
        return "FAILURE", "", f"Could not check whether security group '{name}' exists in {vpc_id}: {_aws_message(exc)}"
    if existing:
        return (
            "FAILURE",
            "",
            f"Security group '{name}' already exists in {vpc_id} ({existing[0]['GroupId']}). Choose another "
            "name, or run discovery (Sync) on this blueprint to adopt it.",
        )

    resource.name = name
    resource.aws_security_group_name = name
    resource.aws_vpc_id = vpc_id
    resource.aws_region = region
    resource.aws_rh_id = rh.id
    resource.save()

    set_progress(f"Creating security group '{name}' in {vpc_id} ({region})")
    try:
        response = client.create_security_group(
            GroupName=name,
            Description=description,
            VpcId=vpc_id,
            TagSpecifications=[
                {"ResourceType": "security-group", "Tags": [{"Key": "Name", "Value": name}]}
            ],
        )
    except (BotoCoreError, ClientError) as exc:
        return "FAILURE", "", f"AWS could not create security group '{name}': {_aws_message(exc)}"

    resource.aws_security_group_id = response["GroupId"]
    resource.save()

    return (
        "SUCCESS",
        f"Security group {name} ({response['GroupId']}) has been created in {vpc_id}.",
        "",
    )
