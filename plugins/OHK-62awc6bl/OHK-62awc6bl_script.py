"""
Discovery plugin for the AWS Security Group blueprint: returns one dictionary
per security group in every region that an AWS resource handler's
environments use, and CloudBolt's discovery sync creates, updates or
reactivates the matching resources from them (servicecatalog/models.py,
ServiceBlueprint.sync_resources). The plugin never creates resources or
parameters itself.

Security groups are matched on their ID and region.

Retirement is the plugin's call; the blueprint's auto_historical_resources
stays off, so a handler or region that cannot be read retires nothing. A
tracked Active security group is marked Historical only when its handler's
listing of its region succeeded, the group is missing from it, no job is
running on it, and a lookup of the group by ID answers InvalidGroup.NotFound.
Anything else returns the identifier alone, which changes nothing. The sync
reactivates a Historical security group when it is found again.
  https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeSecurityGroups.html
  https://docs.aws.amazon.com/AWSEC2/latest/APIReference/errors-overview.html
"""
from botocore.exceptions import ClientError

from common.methods import set_progress
from jobs.models import Job
from resourcehandlers.aws.models import AWSHandler
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

RESOURCE_IDENTIFIER = ["aws_security_group_id", "aws_region"]

# Lifecycles the build gives a resource while it runs and when it fails
# (jobengine/jobmodules/deploy_blueprint_job.py).
BUILDING = "PROV"
BUILD_FAILED = "PROVFAILED"


def _stored(resource):
    return resource.get_cf_values_as_dict()


def _known_resources(blueprint):
    """The blueprint's resources by security group ID, an Active one winning."""
    known = {}
    if blueprint is None:
        return known
    for resource in blueprint.resource_set.all().order_by("id"):
        group_id = _stored(resource).get("aws_security_group_id")
        if group_id and (group_id not in known or resource.lifecycle == "ACTIVE"):
            known[group_id] = resource
    return known


def _untouched(resource):
    """A dictionary that matches the resource and changes nothing."""
    values = _stored(resource)
    return {name: values.get(name) for name in RESOURCE_IDENTIFIER}


def _retired(resource):
    values = _untouched(resource)
    values["lifecycle"] = "HISTORICAL"
    return values


def _has_running_job(resource):
    """A running build, action or teardown may be about to create or delete the group."""
    return (
        resource.jobs.filter(status__in=Job.ACTIVE_STATUSES).exists()
        or Job.objects.filter(
            status__in=Job.ACTIVE_STATUSES,
            job_parameters__hookparameters__resources=resource,
        ).exists()
    )


def _confirmed_gone(client, resource):
    """True only when AWS answers that the security group does not exist."""
    group_id = _stored(resource).get("aws_security_group_id")
    try:
        client.describe_security_groups(GroupIds=[group_id])
        return False
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "InvalidGroup.NotFound":
            return True
        logger.info("Not retiring %s: AWS answered %s", resource, code)
        return False
    except Exception as exc:
        logger.info("Could not confirm whether %s still exists: %s", resource, exc)
        return False


def group_dict(group, rh, region, resource):
    """The values the sync sets on a security group's resource, matching what the build stores."""
    values = {
        "name": group["GroupName"],
        "aws_security_group_name": group["GroupName"],
        "aws_security_group_id": group["GroupId"],
        "aws_vpc_id": group.get("VpcId", ""),
        "aws_region": region,
        "aws_rh_id": str(rh.id),
    }
    if resource is not None:
        values.update(_untouched(resource))
        if resource.lifecycle == BUILD_FAILED:
            # The sync only reactivates Historical resources by itself.
            values["lifecycle"] = "ACTIVE"
            set_progress(f"Recovering '{group['GroupName']}': its CloudBolt build failed after AWS created it.")
    return values


def discover_resources(**kwargs):
    known = _known_resources(kwargs.get("blueprint"))
    discovered = []
    returned = set()
    # Region listings that succeeded, by (handler ID, region), with their EC2 client.
    clients = {}

    for rh in AWSHandler.objects.all():
        try:
            regions = sorted(rh.current_regions())
        except Exception as exc:
            set_progress(f"Skipping AWS handler '{rh.name}': {exc}")
            continue
        for region in regions:
            set_progress(f"Listing security groups for AWS handler '{rh.name}' in {region}")
            try:
                client = rh.get_boto3_client(region, "ec2")
                groups = []
                for page in client.get_paginator("describe_security_groups").paginate():
                    groups.extend(page.get("SecurityGroups", []))
            except Exception as exc:
                set_progress(f"Skipping AWS handler '{rh.name}' in {region}: {exc}")
                continue
            clients[(str(rh.id), region)] = client
            set_progress(f"AWS handler '{rh.name}' in {region}: {len(groups)} security group(s).")

            for group in groups:
                group_id = group["GroupId"]
                # Two handlers on the same account see the same security groups.
                if group_id in returned:
                    continue
                returned.add(group_id)
                resource = known.get(group_id)
                if resource is not None and resource.lifecycle == BUILDING:
                    set_progress(f"Skipping '{group['GroupName']}': its CloudBolt build is still running.")
                    discovered.append(_untouched(resource))
                    continue
                discovered.append(group_dict(group, rh, region, resource))

    # Tracked Active security groups no listing returned.
    for group_id, resource in known.items():
        if resource.lifecycle != "ACTIVE" or group_id in returned:
            continue
        values = _stored(resource)
        client = clients.get((str(values.get("aws_rh_id")), values.get("aws_region")))
        if client is not None and not _has_running_job(resource) and _confirmed_gone(client, resource):
            set_progress(f"Security group '{resource.name}' no longer exists; marking it Historical.")
            discovered.append(_retired(resource))
        else:
            discovered.append(_untouched(resource))

    return discovered
