"""
Data disk mapping: adds data disks to a server and records, for each one, what
the guest operating system can use to find that exact disk.

Used by the Attach Data Disks plug-ins of the "Windows Server with Data Disks"
and "Oracle Linux Server with Data Disks" blueprints. The in-guest Configure Data
Disks scripts never know which hypervisor they run on. They read the server's
data_disk_layout parameter, where every disk carries the same handler-neutral
correlation keys, all optional:

    address      Text for reports, e.g. "vCenter SCSI(0:1)" or "Azure LUN 2".
    serials      Disk identifiers (UUIDs) the guest may see inside the disk's
                 serial number, WWN, or unique ID. The scripts compare hex digits
                 only, at least 16 of them, and accept either value containing the
                 other. Any one matching identifies the disk.
    scsi_target  SCSI target ID the guest reports for the disk.
    scsi_lun     SCSI LUN the guest reports for the disk.
    scsi_index   The disk's position on its SCSI bus when the hypervisor does not
                 say whether the guest sees it as the target (LUN 0) or as the LUN
                 (target 0); the scripts accept either.

The scripts try the serials, then the SCSI address, then attach order (SCSI
address order), and every match must also agree on size.

Supporting another resource handler means adding one DiskProvider subclass and
registering it in PROVIDERS under the handler's type_slug. A provider's
snapshot() returns {stable disk key: correlation keys} for every disk the VM has;
attach_disks() calls it before and after each add, and the one new key is the
disk just added. check_capacity() may refuse an order the VM cannot hold before
any disk is added; add_disk() may override how the handler is called. Handlers
without a provider still get their disks added (through the handler's own
add_disk_to_existing_server) and are matched by attach order and size in the guest.
"""
import json

from common.methods import set_progress
from infrastructure.models import CustomField
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

LAYOUT_FIELD = "data_disk_layout"
CORRELATION_KEYS = ("address", "serials", "scsi_target", "scsi_lun", "scsi_index")


class DiskProvider:
    """Any handler that can add disks after provisioning; no correlation data."""

    label = "hypervisor"

    def add_disk(self, handler, server, size_gb):
        # The call CloudBolt makes for disk_N_size parameters during provisioning
        # (jobengine/jobmodules/provisionjob.py, adjust_disks).
        return handler.add_disk_to_existing_server(server, size_gb, server.custom_field_values.all())

    def snapshot(self, handler, server):
        return None

    def check_capacity(self, handler, server, adding):
        """Raise ValueError when the VM cannot take `adding` more data disks."""


class VMwareProvider(DiskProvider):
    """vCenter: SCSI controller bus and unit number, and the VMDK UUID."""

    label = "vCenter"

    def add_disk(self, handler, server, size_gb):
        # VsphereResourceHandler.add_disk_to_existing_server places the disk on the
        # server's vmware_datastore parameter; without one, use the datastore of
        # the server's first disk.
        if not server.get_value_for_custom_field("vmware_datastore"):
            first = server.disks.exclude(disk_storage=None).order_by("id").first()
            if first:
                return handler.add_disk_to_existing_server(
                    server, size_gb, server.custom_field_values.all(), datastore=first.disk_storage
                )
        return super().add_disk(handler, server, size_gb)

    def snapshot(self, handler, server):
        # get_vm_dict lists each disk's backing uuid, controller_bus, and unit
        # (resourcehandlers/vmware/pyvmomi_wrapper.py, get_vm_details). With
        # disk.EnableUUID = TRUE the guest reports the VMDK UUID as the disk serial
        # (https://knowledge.broadcom.com/external/article/432156). The unit number
        # is the SCSI target ID, and the LUN is 0.
        vm = handler.get_vm_dict(server)
        return {
            d["uuid"]: {
                "address": f"vCenter SCSI({d.get('controller_bus')}:{d.get('unit')})",
                "serials": [d["uuid"]],
                "scsi_target": d.get("unit"),
                "scsi_lun": 0,
            }
            for d in vm.get("disks", [])
            if d.get("uuid")
        }


class AzureProvider(DiskProvider):
    """Azure: the data disk's LUN."""

    label = "Azure"

    def _vm(self, handler, server):
        # The VM lookup AzureARMHandler.add_disk_to_existing_server makes.
        info = handler.tech_specific_server_details(server)
        group = info.resource_group if info and info.resource_group else server.get_vm_name()
        compute = handler.get_api_wrapper().compute_client
        return compute, group, compute.get_vm_object(server.get_vm_name(), group)

    def snapshot(self, handler, server):
        # The guest sees a data disk's LUN as its SCSI LUN:
        # https://learn.microsoft.com/en-us/azure/virtual-machines/linux/add-disk
        # DataDisk: https://learn.microsoft.com/en-us/python/api/azure-mgmt-compute/azure.mgmt.compute.models.datadisk
        _, _, vm = self._vm(handler, server)
        return {
            d.name: {"address": f"Azure LUN {d.lun}", "scsi_lun": d.lun}
            for d in vm.storage_profile.data_disks
        }

    def check_capacity(self, handler, server, adding):
        # Each VM size caps its data disks (VirtualMachineSize.max_data_disk_count):
        # https://learn.microsoft.com/en-us/python/api/azure-mgmt-compute/azure.mgmt.compute.models.virtualmachinesize
        # list_available_sizes is the call AzureARMHandler makes for resize options.
        compute, group, vm = self._vm(handler, server)
        size = vm.hardware_profile.vm_size
        limit = next(
            (s.max_data_disk_count for s in compute.virtual_machines.list_available_sizes(group, vm.name)
             if s.name == size),
            None,
        )
        attached = len(vm.storage_profile.data_disks)
        if limit is not None and attached + adding > limit:
            raise ValueError(
                f"Azure size {size} takes at most {limit} data disks; the VM has {attached} and "
                f"{adding} more were ordered. Order a larger size or fewer disks."
            )


class NutanixProvider(DiskProvider):
    """Nutanix AHV through Prism Central. Written from the v4 API reference; untested on a cluster."""

    label = "Nutanix AHV"

    def snapshot(self, handler, server):
        wrapper = handler.get_api_wrapper()
        if not getattr(wrapper, "is_api_v4", False):
            return self._snapshot_legacy(handler, server)
        # GET vmm/<version>/ahv/config/vms/<extId>, the call AcropolisWrapper.get_vm
        # makes. CloudBolt's own disk summary keeps only Disk.extId, so read the raw
        # record. Field meanings, from Nutanix's v4 SDK (ntnx-api-golang-clients,
        # vmm-go-client models/vmm/v4/ahv/config):
        #   Disk.extId               the disk's API instance ID
        #   Disk.diskAddress         busType (SCSI, IDE, PCI, SATA, SPAPR) and index,
        #                            "Device index on the bus"
        #   VmDisk.diskExtId         "A globally unique identifier of a VM disk"
        #                            (the vmdisk UUID); in every v4 version
        #   VmDisk.serialId          "The disk serial ID is used to identify the disk
        #                            in the hypervisor"; API v4.3 and later only
        # An AHV guest's disk serial carries the vmdisk UUID (underscores for
        # hyphens): https://next.nutanix.com/ahv-virtualization-27/map-ahv-vm-drives-to-os-41002
        # All three IDs are offered; each is a random UUID, so any match is safe.
        # Nothing public says whether the guest reports the bus index as the SCSI
        # target or the LUN, so it is passed as scsi_index and the scripts accept
        # either.
        vm = wrapper.client.get(
            f"vmm/{wrapper.api_version}/ahv/config/vms/{server.resource_handler_svr_id}"
        ).get("data", {})
        disks = {}
        for d in vm.get("disks") or []:
            backing = d.get("backingInfo") or {}
            if backing.get("$objectType") != "vmm.v4.ahv.config.VmDisk" or not d.get("extId"):
                continue  # volume group disks and other backings are not data disks
            bus = str((d.get("diskAddress") or {}).get("busType") or "").upper()
            index = (d.get("diskAddress") or {}).get("index")
            ids = [backing.get("serialId"), backing.get("diskExtId"), d.get("extId")]
            fields = {
                "address": f"Nutanix {bus.lower() or 'disk'}.{index}",
                "serials": list(dict.fromkeys(i for i in ids if i)),
            }
            if bus == "SCSI" and index is not None:
                fields["scsi_index"] = int(index)
            disks[d["extId"]] = fields
        return disks

    def _snapshot_legacy(self, handler, server):
        # Prism v2/v3 through CloudBolt's own disk summary: uuid, adapter_type,
        # device_index (acropolis_wrapper.py, _convert_disks).
        disks = {}
        for d in handler.get_vm_dict(server).get("disks", []):
            if not d.get("uuid"):
                continue
            bus = str(d.get("adapter_type") or "").upper()
            index = d.get("device_index")
            fields = {"address": f"Nutanix {bus.lower() or 'disk'}.{index}", "serials": [d["uuid"]]}
            if bus == "SCSI" and index is not None:
                fields["scsi_index"] = int(index)
            disks[d["uuid"]] = fields
        return disks


# type_slug of the resource handler -> provider
PROVIDERS = {
    "vmware": VMwareProvider(),
    "azure_arm": AzureProvider(),
    "acropolis": NutanixProvider(),
}


def provider_for(handler):
    return PROVIDERS.get(handler.type_slug, DiskProvider())


def ensure_layout_field():
    CustomField.objects.get_or_create(
        name=LAYOUT_FIELD,
        defaults=dict(
            label="Data Disk Layout",
            type="TXT",
            show_on_servers=True,
            description=(
                "Data disks CloudBolt attached to this server, as JSON: one entry per disk with "
                "its in-guest target, size, volume name, and the keys the guest uses to find it."
            ),
        ),
    )


def _layout_for(server, disks, compare_keys):
    """Return the saved layout when it matches this order, else a fresh one."""
    raw = server.get_value_for_custom_field(LAYOUT_FIELD)
    if not raw:
        return [dict(d, attached=False) for d in disks]
    try:
        saved = json.loads(raw)
    except ValueError:
        saved = None
    if (
        isinstance(saved, list)
        and len(saved) == len(disks)
        and all(all(s.get(k) == d.get(k) for k in compare_keys) for s, d in zip(saved, disks))
    ):
        return saved
    raise ValueError(
        f"the server already has a different {LAYOUT_FIELD}; remove the extra disks and the "
        "parameter before running this item again"
    )


def _snapshot(provider, handler, server):
    try:
        return provider.snapshot(handler, server)
    except Exception:
        logger.exception("Could not read the disks of %s from %s", server.hostname, handler.type_slug)
        return None


def address(disk):
    return disk.get("address") or "address not reported"


def attach_disks(server, disks, describe):
    """
    Add one disk per entry of `disks` (dicts with disk, size_gb, and the OS
    target and volume name) to `server` in order, save the layout with each
    disk's correlation keys on the server, and return the layout. Entries already
    marked attached in the saved layout are skipped, so a rerun resumes.
    """
    handler = server.resource_handler.cast()
    if not getattr(handler, "can_add_disks_after_prov", False):
        raise ValueError(f"{handler.type_slug} resource handlers cannot add disks to a server")
    provider = provider_for(handler)
    layout = _layout_for(server, disks, [k for k in disks[0] if k != "attached"])
    # Fail before attaching anything when the VM cannot take every disk. A lookup
    # error is logged, not fatal: the add itself still reports a real limit.
    try:
        provider.check_capacity(handler, server, sum(1 for d in layout if not d.get("attached")))
    except ValueError:
        raise
    except Exception:
        logger.exception("Could not check the data disk capacity of %s", server.hostname)

    for disk in layout:
        if disk.get("attached"):
            set_progress(f"{server.hostname}: disk {disk['disk']} ({describe(disk)}) is already attached")
            continue
        before = _snapshot(provider, handler, server)
        set_progress(f"{server.hostname}: adding disk {disk['disk']} ({describe(disk)})")
        provider.add_disk(handler, server, disk["size_gb"])
        disk["attached"] = True

        if before is not None:
            after = _snapshot(provider, handler, server) or {}
            new = [fields for key, fields in after.items() if key not in before]
            if len(new) == 1:
                disk.update({k: v for k, v in new[0].items() if k in CORRELATION_KEYS and v is not None})
                set_progress(f"{server.hostname}: disk {disk['disk']} is at {address(disk)}")
            else:
                set_progress(
                    f"{server.hostname}: {provider.label} shows {len(new)} new disks after adding disk "
                    f"{disk['disk']}; the guest script will match it by attach order and size"
                )
        # Save after every disk so a rerun resumes. Compact JSON: the Windows script
        # embeds it, and Azure Run Command caps the script's encoded length.
        server.set_value_for_custom_field(LAYOUT_FIELD, json.dumps(layout, separators=(",", ":")))
    return layout
