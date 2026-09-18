"""
CloudBolt orchestration plugin (Post-Provision): attach each provisioned Azure
VM to the Network Security Group the user selected on the order form.

Pairing: the `azure_nsg` parameter is populated by the "Azure NSG - Generate
Options" plug-in (OHK-vswf8b1m), whose option *values* are NSG Resource IDs.
That selection lands on the server as the `azure_nsg` custom field. This plugin:

  - iterates the job's servers (Post-Provision passes NO `server` kwarg, so use
    job.server_set.all() — mirrors the OOTB hook pattern);
  - for each server, reads `azure_nsg`. If it is empty, there is nothing to do
    for that server, so it is skipped. If NO server has a value, the plugin
    exits SUCCESS (the feature is simply unused on this order);
  - if a value is present, associates the VM's network interface(s) with that
    NSG by setting the NIC's networkSecurityGroup to the NSG Resource ID and
    updating the NIC.

In Azure a VM is "added to" an NSG by attaching the NSG to the VM's NIC (or its
subnet). This plugin uses the per-VM NIC association, which scopes the NSG to
exactly this VM. Docs:
  https://learn.microsoft.com/en-us/azure/virtual-network/network-security-group-how-it-works
  https://learn.microsoft.com/en-us/azure/virtual-network/manage-network-security-group#associate-or-dissociate-a-network-security-group

External API: Azure Compute + Network Resource Providers via azure-mgmt-compute
and azure-mgmt-network, authenticated through CloudBolt's configure_arm_client
wrapper. Operation shapes anchored to Microsoft's current docs (cited at each
call site) per docs/agents/external-apis.md — not extrapolated from memory.

Entry point: run(job, *args, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from infrastructure.models import CustomField
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The parameter the user selected on the order form (value = NSG Resource ID),
# and where we record what we actually attached for audit/idempotency.
CF_NSG = "azure_nsg"
CF_NSG_APPLIED = "azure_nsg_applied_id"


def _ensure_custom_fields():
    """Idempotently create the fields this plugin reads/writes.

    `azure_nsg` is normally defined by the blueprint/order form that wires in the
    options plug-in; creating it here as well is harmless and lets the plugin run
    standalone. get_or_create keeps it idempotent.
    """
    CustomField.objects.get_or_create(
        name=CF_NSG,
        defaults=dict(
            label="Azure Network Security Group",
            description="Resource ID of the Azure NSG to attach the VM's NIC to.",
            type="STR",
            show_on_servers=True,
        ),
    )
    CustomField.objects.get_or_create(
        name=CF_NSG_APPLIED,
        defaults=dict(
            label="Azure NSG Applied (Resource ID)",
            description="Resource ID of the Azure NSG this server's NIC was attached to.",
            type="STR",
            show_on_servers=True,
        ),
    )


def _azure_vm_coords(server):
    """Return (resource_group, vm_name) for the server's Azure VM.

    Confirmed against platform source: AzureARMServerInfo.resource_group
    (azure_arm/models.py:3990) holds the resource group, and the VM's Azure name
    is taken to equal server.hostname — the azure_object property
    (azure_arm/models.py:4005) calls
    get_vm_object(resource_group_name=self.resource_group, vm_name=self.server.hostname).
    At Post-Provision the VM was just created by CloudBolt, so hostname == VM name
    holds; the drift caveat only applies to out-of-band imports/renames.
    """
    info = getattr(server, "azurearmserverinfo", None)
    resource_group = getattr(info, "resource_group", None)
    vm_name = server.hostname
    return resource_group, vm_name


def _parse_nic_coords(nic_id):
    """Parse (resource_group, nic_name) out of an Azure NIC Resource ID.

    Format: /subscriptions/{sub}/resourceGroups/{rg}/providers/
            Microsoft.Network/networkInterfaces/{nic}
    Index layout per docs/agents/common-patterns.md (Parsing Azure Resource IDs).
    """
    parts = nic_id.split("/")
    return parts[4], parts[8]


def run(job, *args, **kwargs):
    servers = list(job.server_set.all())
    if not servers:
        return "WARNING", "No servers on this job; no NSG to attach.", ""

    
    _ensure_custom_fields()

    applied, skipped, failures = [], [], []
    for server in servers:
        nsg_id = (server.get_value_for_custom_field(CF_NSG) or "").strip()
        set_progress(f"Azure NSG: attaching NSG ID: {nsg_id} to {server.hostname}.")
        if not nsg_id:
            # No NSG selected for this server — nothing to do.
            skipped.append(getattr(server, "hostname", "?"))
            continue
        try:
            applied.append(_attach_nsg(server, nsg_id))
        except Exception as exc:  # noqa: BLE001 — surface, don't abort other servers
            msg = "%s: %s" % (getattr(server, "hostname", "?"), exc)
            logger.exception("Azure NSG attach failed for %s", getattr(server, "hostname", "?"))
            set_progress(msg)
            failures.append(msg)

    if failures:
        summary = "; ".join(applied) if applied else "no NICs updated"
        return "FAILURE", summary, " | ".join(failures)

    if not applied:
        # Task contract: if azure_nsg is not set on any server, exit successfully.
        return "SUCCESS", "No azure_nsg selected; nothing to attach.", ""

    return "SUCCESS", "; ".join(applied), ""


def _attach_nsg(server, nsg_id):
    """Attach `nsg_id` to every NIC on the server's Azure VM."""
    from resourcehandlers.azure_arm.models import AzureARMHandler

    rh = server.resource_handler.cast() if server.resource_handler else None
    if not isinstance(rh, AzureARMHandler):
        raise ValueError("server is not backed by an Azure resource handler")

    resource_group, vm_name = _azure_vm_coords(server)
    if not resource_group or not vm_name:
        raise ValueError("could not resolve Azure resource group / VM name for server")

    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.network.models import NetworkSecurityGroup
    from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client

    wrapper = rh.get_api_wrapper()
    compute_client = configure_arm_client(wrapper, ComputeManagementClient)
    network_client = configure_arm_client(wrapper, NetworkManagementClient)

    # The canonical Azure NIC resource name + its resource group are not reliably
    # stored on CloudBolt's ServerNetworkCard (only ip/mac/index/local name are),
    # so the supported path is to read the NIC references off the VM's network
    # profile via the SDK.
    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-compute/azure.mgmt.compute.operations.virtualmachinesoperations#get
    #       VirtualMachinesOperations.get(resource_group_name, vm_name) -> VirtualMachine
    #       (vm.network_profile.network_interfaces -> [NetworkInterfaceReference(.id)]).
    vm = compute_client.virtual_machines.get(resource_group, vm_name)
    nic_refs = (getattr(vm.network_profile, "network_interfaces", None) or []) if vm.network_profile else []
    if not nic_refs:
        raise ValueError(f"VM '{vm_name}' has no network interfaces")

    updated_nics = []
    for nic_ref in nic_refs:
        nic_rg, nic_name = _parse_nic_coords(nic_ref.id)

        # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networkinterfacesoperations#get
        #       NetworkInterfacesOperations.get(resource_group_name, network_interface_name) -> NetworkInterface
        nic = network_client.network_interfaces.get(nic_rg, nic_name)

        current = getattr(nic.network_security_group, "id", None) if nic.network_security_group else None
        if current == nsg_id:
            updated_nics.append(f"{nic_name} (already attached)")
            continue

        # Attach the NSG by Resource ID and persist the NIC.
        # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networkinterfacesoperations#begin-create-or-update
        #       begin_create_or_update(resource_group_name, network_interface_name, parameters) -> LROPoller[NetworkInterface]
        nic.network_security_group = NetworkSecurityGroup(id=nsg_id)
        network_client.network_interfaces.begin_create_or_update(nic_rg, nic_name, nic).result()
        updated_nics.append(nic_name)

    server.set_value_for_custom_field(CF_NSG_APPLIED, nsg_id)
    nsg_name = nsg_id.rsplit("/", 1)[-1]
    msg = "%s: NSG '%s' attached to NIC(s) %s" % (
        getattr(server, "hostname", vm_name), nsg_name, ", ".join(updated_nics),
    )
    logger.info(msg)
    set_progress(msg)
    return msg
