"""
Build item for the AWS EKS blueprint (BP-r5apcf7e): create the cluster's first
managed node group, so workloads and the StormForge agent installed by the
next item have nodes to run on.

Runs after Deploy EKS Cluster, waits until the node group is ACTIVE, then
waits up to ten minutes for its nodes to be Ready in Kubernetes. A node group
that fails fails the order; nodes slow to turn Ready are only a warning.

The cluster's environment, Kubernetes version, and subnets are inputs of the
Deploy EKS Cluster item, and CloudBolt resolves an order-form dependency only
between inputs of the same item, so AMI Type and Instance Type here are fixed
lists instead of being read from the cluster as the Add AWS EKS Nodegroup
action (RSA-f9ivvn5q) does. Before creating anything, run checks the instance
type against the AMI's architecture and the cluster's availability zones.

The work is in shared_modules/SHM-uj1izdsm (eks_nodegroups), shared with that
action. This item has its own plugin because one plugin bound to both a
resource action and a build item shares its input defaults between them,
which breaks the action's sync.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from shared_modules import eks_nodegroups
from shared_modules.eks_nodegroups import AMI_FAMILIES

DEFAULT_AMI_TYPE = "x86_64/AL2023_x86_64_STANDARD"

# Widely offered general-purpose, compute, and memory sizes per architecture,
# from 2 vCPU and 4 GiB up (t3.small is too small for the StormForge agent's
# pods). Instance types: https://aws.amazon.com/ec2/instance-types/
INSTANCE_TYPES = {
    "x86_64": [
        ("t3.medium", 2, 4),
        ("t3.large", 2, 8),
        ("t3.xlarge", 4, 16),
        ("t3.2xlarge", 8, 32),
        ("m5.large", 2, 8),
        ("m5.xlarge", 4, 16),
        ("m5.2xlarge", 8, 32),
        ("m6i.large", 2, 8),
        ("m6i.xlarge", 4, 16),
        ("m6i.2xlarge", 8, 32),
        ("c5.xlarge", 4, 8),
        ("c6i.xlarge", 4, 8),
        ("r5.large", 2, 16),
        ("r6i.large", 2, 16),
    ],
    "arm64": [
        ("t4g.medium", 2, 4),
        ("t4g.large", 2, 8),
        ("t4g.xlarge", 4, 16),
        ("t4g.2xlarge", 8, 32),
        ("m6g.large", 2, 8),
        ("m6g.xlarge", 4, 16),
        ("m6g.2xlarge", 8, 32),
        ("c6g.xlarge", 4, 8),
        ("r6g.large", 2, 16),
    ],
}


def generate_options_for_ami_type(**kwargs):
    """
    Amazon Linux 2023 and Bottlerocket, which AWS publishes for every
    Kubernetes version EKS supports. Amazon Linux 2 is left out: it ended
    with 1.32.
    """
    options = [
        (f"{architecture}/{ami_type}", f"{label} ({ami_type})")
        for ami_type, architecture, label, _ in AMI_FAMILIES
        if not ami_type.startswith("AL2_")
    ]
    return {"options": options, "initial_value": DEFAULT_AMI_TYPE, "sort": False}


def generate_options_for_instance_type(control_value=None, **kwargs):
    """Instance types for the architecture of the chosen AMI type."""
    if not control_value:
        return [("", "------ First, select an AMI type ------")]
    architecture = str(control_value).split("/")[0]
    sizes = INSTANCE_TYPES.get(architecture, [])
    options = [(name, f"{name} ({vcpu} vCPU, {memory} GiB)") for name, vcpu, memory in sizes]
    return {"options": options, "initial_value": sizes[0][0] if sizes else None, "sort": False}


def run(job, resource=None, **kwargs):
    resource = resource or kwargs.get("resource")
    if resource is None and job is not None:
        resource = job.resource_set.first()
    if resource is None:
        return "FAILURE", "", "No cluster resource was supplied."

    # A blank optional input renders as nothing, or as None when the order
    # passed it empty.
    nodegroup_name = "{{ nodegroup_name }}".strip()
    if nodegroup_name in ("", "None"):
        nodegroup_name = eks_nodegroups.default_nodegroup_name(resource)
    try:
        nodegroup = eks_nodegroups.add_nodegroup(
            job,
            resource,
            nodegroup_name=nodegroup_name,
            ami_type="{{ ami_type }}",
            instance_type="{{ instance_type }}",
            disk_size="{{ disk_size }}",
            min_size="{{ min_nodes }}",
            max_size="{{ max_nodes }}",
            desired_size="{{ desired_nodes }}",
        )
    except RuntimeError as err:
        return "FAILURE", "", str(err)

    # ACTIVE is AWS's view; the StormForge item next needs Ready nodes.
    note = eks_nodegroups.wait_for_ready_nodes(resource, nodegroup["scalingConfig"]["desiredSize"])
    if note:
        return "WARNING", f"Node group {nodegroup['nodegroupName']} is active", note
    return "SUCCESS", f"Node group {nodegroup['nodegroupName']} is active", ""
