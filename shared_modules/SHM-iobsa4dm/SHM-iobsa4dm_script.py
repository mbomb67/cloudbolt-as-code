"""
Data disk mapping: adds data disks to a server and records, for each one, what
the guest operating system can use to find that exact disk.

Used by the Attach Data Disks plug-ins of the "Windows Server with Data Disks"
and "Oracle Linux Server with Data Disks" blueprints. The in-guest Configure Data
Disks scripts never know which hypervisor they run on. They read the server's
data_disk_layout parameter, where every disk carries the same handler-neutral
correlation keys, all optional:

    address      Text for reports, e.g. "vCenter SCSI(0:1)" or "Azure LUN 2".
    serial       A disk identifier the guest sees inside the disk's serial number
                 or WWN. The scripts compare hex digits only and accept a guest
                 value that contains it.
    scsi_target  SCSI target ID the guest reports for the disk.
    scsi_lun     SCSI LUN the guest reports for the disk.

The scripts try serial, then the SCSI target and LUN together, then attach order,
and every match must also agree on size.

Supporting another resource handler means adding one DiskProvider subclass and
registering it in PROVIDERS under the handler's type_slug. A provider's
snapshot() returns {stable disk key: correlation keys} for every disk the VM has;
attach_disks() calls it before and after each add, and the one new key is the
disk just added. check_capacity() may refuse an order the VM cannot hold before
any disk is added; add_disk() may override how the handler is called. Handlers without a provider still get their disks added (through
the handler's own add_disk_to_existing_server) and are matched by attach order and
size in the guest.
"""
import json

from common.methods import set_progress
from infrastructure.models import CustomField
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

LAYOUT_FIELD = "data_disk_layout"
CORRELATION_KEYS = ("address", "serial", "scsi_target", "scsi_lun")


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
                "serial": d["uuid"],
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
    """Nutanix AHV: the disk's SCSI device index and UUID. Untested on a live cluster."""

    label = "Nutanix AHV"

    def snapshot(self, handler, server):
        # The acropolis handler's get_vm_dict lists each disk's uuid, adapter_type,
        # and device_index (resourcehandlers/acropolis/acropolis_wrapper.py,
        # _convert_disks). AHV presents disks through virtio-scsi, which on KVM
        # exposes each disk index as a LUN on target 0; if a guest reports it
        # differently, the address check finds nothing and the scripts fall back to
        # attach order. The guest disk serial embeds the vmdisk UUID
        # (https://next.nutanix.com/ahv-virtualization-27/map-ahv-vm-drives-to-os-41002);
        # the uuid CloudBolt reports is offered as the serial and matches when it is
        # that vmdisk UUID.
        vm = handler.get_vm_dict(server)
        disks = {}
        for d in vm.get("disks", []):
            if not d.get("uuid"):
                continue
            bus = str(d.get("adapter_type") or "").upper()
            index = d.get("device_index")
            fields = {"address": f"Nutanix {bus.lower() or 'disk'}.{index}", "serial": d["uuid"]}
            if bus == "SCSI" and index is not None:
                fields.update(scsi_target=0, scsi_lun=int(index))
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
