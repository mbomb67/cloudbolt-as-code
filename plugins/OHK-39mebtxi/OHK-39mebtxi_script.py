"""
This is a working sample CloudBolt plug-in for you to start with. The run method is required,
but you can change all the code within it. See the "CloudBolt Plug-ins" section of the docs for
more info and the CloudBolt forge for more examples:
https://github.com/CloudBoltSoftware/cloudbolt-forge/tree/master/actions/cloudbolt_plugins
"""

from infrastructure.models import CustomField, Environment
from resourcehandlers.aws.models import AWSHandler
from resources.models import Resource
from utilities.logger import ThreadLogger
from common.methods import set_progress

logger = ThreadLogger(__name__)


def get_or_create_custom_fields():
    """
    get_or_create_custom_fields
    """

    CustomField.objects.get_or_create(
        name="delete_nodegroup",
        defaults={
            "label": "Delete Nodegroup",
            "type": "STR",
            "description": "Used by the Eks blueprints",
            "required": True,
            "allow_multiple": True,
        },
    )


def generate_options_for_nodegroup_resource_status(server=None, **kwargs):
    """
    generate_options_for_eks_nodegroup_resource_status
    """
    resource = kwargs.get("resource", None)
    options = []

    if resource is None:
        return options

    return [
        ("", "------Select resource status-------"),
        (f"ACTIVE/{resource.id}", "ACTIVE"),
        (f"UPDATING/{resource.id}", "UPDATING"),
        (f"CREATE_FAILED/{resource.id}", "CREATE_FAILED"),
        (f"DELETE_FAILED/{resource.id}", "DELETE_FAILED"),
        (f"DEGRADED/{resource.id}", "DEGRADED"),
    ]


def generate_options_for_delete_nodegroup(server=None, control_value=None, **kwargs):
    """
    Generate delete nodegroup options
    Dependency - eks_nodegroup_resource_status
    """
    options = []

    if not control_value:
        return options

    control_value = control_value.split("/")

    sub_resources = Resource.objects.filter(
        parent_resource_id=control_value[1], lifecycle="ACTIVE"
    )

    for sub_resource in sub_resources:

        nodegroup_cf = sub_resource.get_cf_values_as_dict()
        if (
            "eks_nodegroup_resource_status" in nodegroup_cf
            and nodegroup_cf["eks_nodegroup_resource_status"] == control_value[0]
        ):
            options.append((sub_resource.id, sub_resource.name))

    return options


def get_boto_client(env_id=None, boto_service=""):
    """
    get_boto_client
    """
    if env_id is None:
        return None
    env = Environment.objects.get(id=env_id)
    rh: AWSHandler = env.resource_handler.cast()

    # get aws client object
    client = rh.get_boto3_client(env.aws_region, boto_service)

    return client, env


def delete_nodegroup(job, nodegroup_object, resource, client):
    """
    delete_nodegroup
    """
    try:
        # verify nodegroup
        client.describe_nodegroup(
            clusterName=resource.name, nodegroupName=nodegroup_object.name
        )
    except Exception as err:
        if "ResourceNotFound" in str(err):
            return
        raise RuntimeError(err)

    job.set_progress("Deleting nodegroup {0}...".format(nodegroup_object.name))

    try:
        ng_delete_resp = client.delete_nodegroup(
            clusterName=resource.name, nodegroupName=nodegroup_object.name
        )
    except Exception as err:
        raise RuntimeError(
            f"Amazon EKS cluster nodegroup could not be deleted. Error: {err}"
        )

    try:
        # It takes a while for the nodegroup to be deleted.
        nodegroup_delete_waiter = client.get_waiter("nodegroup_deleted")
        nodegroup_delete_waiter.wait(
            clusterName=resource.name, nodegroupName=nodegroup_object.name
        )
    except Exception as err:
        raise RuntimeError(
            f"Amazon EKS cluster nodegroup could not be deleted. Error: {err}"
        )

    logger.info(f"nodegroup response {ng_delete_resp}")


def run(job, resource, **kwargs):
    set_progress(f"Starting Provision of {resource} delete nodegroup.")
    logger.info(f"Starting Provision of {resource} delete nodegroup.")

    # get or create custom fields
    get_or_create_custom_fields()

    nodegroup_resource_status = "{{nodegroup_resource_status}}"
    delete_nodegroup_object = "{{delete_nodegroup}}"

    client, _ = get_boto_client(resource.env_id, "eks")

    nodegroup_object = Resource.objects.get(id=int(delete_nodegroup_object))

    logger.info(f"nodegroup object : {delete_nodegroup_object}")

    # delete node group
    delete_nodegroup(job, nodegroup_object, resource, client)

    nodegroup_object.delete()

    set_progress(f"Node Group {nodegroup_object} deleted successfully.")

    return "SUCCESS", f"Node Group  {nodegroup_object} deleted successfully.", ""