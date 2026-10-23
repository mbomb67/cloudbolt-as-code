"""
Deletes the AWS security group behind the resource. AWS refuses to delete a
security group that is still attached to a network interface or referenced
by another security group's rules, and a VPC's default security group; the
teardown then fails with AWS's reason, naming the network interfaces that
still use the group. A security group that is already gone counts as deleted.

AWS is reached through the resource handler recorded on the resource
(aws_rh_id) in the recorded region (aws_region).
  https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DeleteSecurityGroup.html
  https://docs.aws.amazon.com/AWSEC2/latest/APIReference/errors-overview.html
  https://docs.aws.amazon.com/vpc/latest/userguide/deleting-security-groups.html
"""
from botocore.exceptions import BotoCoreError, ClientError

from common.methods import set_progress
from resourcehandlers.aws.models import AWSHandler


def _resource(job, kwargs):
    resource = kwargs.get("resource")
    if resource is None and kwargs.get("resources") is not None:
        resource = kwargs["resources"].first()
    return resource or job.resource_set.first()


def _error_code(exc):
    return exc.response.get("Error", {}).get("Code", "") if isinstance(exc, ClientError) else ""


def _aws_message(exc):
    """AWS's error code and message, else the SDK's text."""
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return "{} ({})".format(error.get("Message", exc), error.get("Code", "ClientError"))
    return str(exc)


def _interfaces_using(client, group_id):
    """IDs of the network interfaces the security group is attached to, or [] when they cannot be listed.
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeNetworkInterfaces.html
    """
    try:
        response = client.describe_network_interfaces(Filters=[{"Name": "group-id", "Values": [group_id]}])
    except (BotoCoreError, ClientError):
        return []
    return [eni["NetworkInterfaceId"] for eni in response.get("NetworkInterfaces", [])]


def run(job, **kwargs):
    resource = _resource(job, kwargs)
    if resource is None:
        return "FAILURE", "", "No resource is attached to this teardown job."

    values = resource.get_cf_values_as_dict()
    group_id = values.get("aws_security_group_id")
    if not group_id:
        return "SUCCESS", "No AWS security group is recorded on this resource; nothing to delete.", ""

    name = values.get("aws_security_group_name") or group_id
    region = values.get("aws_region")
    rh = AWSHandler.objects.filter(id=values.get("aws_rh_id")).first()
    if rh is None:
        return (
            "FAILURE",
            "",
            f"The AWS resource handler recorded on this resource (ID {values.get('aws_rh_id')}) no longer "
            f"exists, so security group '{name}' ({group_id}) cannot be deleted. Delete it in AWS, then "
            "delete this resource again.",
        )

    set_progress(f"Deleting security group '{name}' ({group_id}) in {region}")
    try:
        client = rh.get_boto3_client(region, "ec2")
        client.delete_security_group(GroupId=group_id)
    except (BotoCoreError, ClientError) as exc:
        code = _error_code(exc)
        if code == "InvalidGroup.NotFound":
            return "WARNING", f"Security group '{name}' ({group_id}) was already gone from AWS.", ""
        message = f"AWS could not delete security group '{name}' ({group_id}): {_aws_message(exc)}"
        if code == "DependencyViolation":
            interfaces = _interfaces_using(client, group_id)
            if interfaces:
                message += " It is attached to network interface(s) " + ", ".join(interfaces[:10]) + "."
            message += (
                " Detach it from every instance and network interface and remove the rules in other "
                "security groups that reference it, then delete this resource again."
            )
        return "FAILURE", "", message

    return "SUCCESS", f"Security group '{name}' ({group_id}) has been deleted.", ""
