"""
Attach Data Disks (Windows)

Build item of the "Windows Server with Data Disks" blueprint. It runs after the
server tier has built and powered on the VM, and before the in-guest
Configure Data Disks (Windows) script.

For each row of the order form's Data Disks table (drive letter, size in GB,
volume name), in row order, on every Windows server of the resource:
1. Adds a virtual disk of that size through the server's resource handler with
   add_disk_to_existing_server, the call CloudBolt itself makes for disk_N_size
   parameters during provisioning (jobengine/jobmodules/provisionjob.py,
   adjust_disks). Any handler that can add disks after provisioning works.
2. Reads the VM's disks back from the hypervisor and records where the new
   disk landed: on vCenter the SCSI controller bus number, unit number, and
   VMDK UUID (VsphereResourceHandler.get_vm_dict); on Azure the data disk's
   LUN (the VM's storage profile, read through CloudBolt's Azure wrapper).
3. Saves every row, with that address, on the server as JSON in the
   data_disk_layout parameter. The in-guest script reads that parameter to find
   each disk by serial number, SCSI target, or LUN before it formats anything.

Running the job again does not add disks twice: rows already marked attached in
the server's data_disk_layout are skipped.
"""
import ast
import json
import re

from common.methods import set_progress
from infrastructure.models import CustomField
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The order form's Data Disks table: one dict per row. CloudBolt escapes quotes
# in action inputs before rendering, so the value cannot end the string early.
DATA_DISKS = """{{ data_disks }}"""

LAYOUT_FIELD = "data_disk_layout"
TARGET_KEY = "drive_letter"
MAX_SIZE_GB = 16384  # NTFS limit with the default 4 KB cluster size
DRIVE_LETTER_RE = re.compile(r"^[D-Z]$")  # A and B are legacy floppy letters, C is the OS
VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,31}$")  # NTFS labels: 32 characters


def _parse_rows(raw):
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("no rows were submitted; add at least one disk")
    try:
        rows = json.loads(raw)
    except ValueError:
        # A table submitted from the form is stored as a Python literal.
        rows = ast.literal_eval(raw)
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        raise ValueError("expected a list of rows, one per disk")
    return rows


def _size_gb(value, number):
    try:
        size = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"disk {number}: size '{value}' is not a number")
    if size != int(size) or not 1 <= size <= MAX_SIZE_GB:
        raise ValueError(f"disk {number}: size must be a whole number of GB from 1 to {MAX_SIZE_GB}")
    return int(size)


def _validate_rows(rows):
    disks, letters = [], set()
    for number, row in enumerate(rows, start=1):
        letter = str(row.get("drive_letter") or "").strip().rstrip(":").upper()
        name = str(row.get("volume_name") or "").strip()
        size = _size_gb(row.get("size_gb"), number)
        if not DRIVE_LETTER_RE.match(letter):
            raise ValueError(f"disk {number}: drive letter '{letter}' must be one of D to Z")
        if letter in letters:
            raise ValueError(f"disk {number}: drive letter {letter}: is already used by another disk")
        if not VOLUME_NAME_RE.match(name):
            raise ValueError(
                f"disk {number}: volume name '{name}' must be 1 to 32 letters, digits, spaces, "
                "hyphens or underscores, starting with a letter or digit"
            )
        letters.add(letter)
        disks.append({"disk": number, "drive_letter": letter, "size_gb": size, "volume_name": name})
    return disks


def _describe(disk):
    return f"{disk['drive_letter']}: {disk['size_gb']} GB '{disk['volume_name']}'"


def _ensure_layout_field():
    CustomField.objects.get_or_create(
        name=LAYOUT_FIELD,
        defaults=dict(
            label="Data Disk Layout",
            type="TXT",
            show_on_servers=True,
            description=(
                "Data disks CloudBolt attached to this server, as JSON: one entry per disk with "
                "its in-guest target, size, volume name, and where the hypervisor attached it "
                "(vCenter SCSI address and VMDK UUID, or Azure LUN)."
            ),
        ),
    )


def _same_disk(saved, wanted):
    keys = ("disk", TARGET_KEY, "size_gb", "volume_name")
    return all(saved.get(k) == wanted.get(k) for k in keys)


def _layout_for(server, disks):
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
        and all(_same_disk(s, d) for s, d in zip(saved, disks))
    ):
        return saved
    raise ValueError(
        f"the server already has a different {LAYOUT_FIELD}; remove the extra disks and the "
        "parameter before running this item again"
    )


def _hypervisor_disks(handler, server):
    """
    Map each disk the hypervisor reports for the VM to the address fields saved
    in data_disk_layout, or return None when the handler is not supported here.
    """
    try:
        if handler.type_slug == "vmware":
            vm = handler.get_vm_dict(server)
            return {
                d["uuid"]: {
                    "scsi_bus": d.get("controller_bus"),
                    "scsi_unit": d.get("unit"),
                    "controller": d.get("controller_summary"),
                    "disk_uuid": d["uuid"],
                }
                for d in vm.get("disks", [])
                if d.get("uuid")
            }
        if handler.type_slug == "azure_arm":
            # The same lookup AzureARMHandler.add_disk_to_existing_server makes.
            # DataDisk: https://learn.microsoft.com/en-us/python/api/azure-mgmt-compute/azure.mgmt.compute.models.datadisk
            info = handler.tech_specific_server_details(server)
            group = info.resource_group if info and info.resource_group else server.get_vm_name()
            vm = handler.get_api_wrapper().compute_client.get_vm_object(server.get_vm_name(), group)
            return {
                d.name: {"lun": d.lun, "azure_disk_name": d.name}
                for d in vm.storage_profile.data_disks
            }
    except Exception:
        logger.exception("Could not read the disks of %s from %s", server.hostname, handler.type_slug)
    return None


def _address(disk):
    if disk.get("scsi_unit") is not None:
        return f"SCSI({disk['scsi_bus']}:{disk['scsi_unit']})"
    if disk.get("lun") is not None:
        return f"LUN {disk['lun']}"
    return "address not reported"


def _add_disk(handler, server, size_gb):
    cfvs = server.custom_field_values.all()
    if handler.type_slug == "vmware" and not server.get_value_for_custom_field("vmware_datastore"):
        # The vCenter handler places the disk on the server's vmware_datastore
        # parameter. Without one, use the datastore of the server's first disk.
        first = server.disks.exclude(disk_storage=None).order_by("id").first()
        if first:
            return handler.add_disk_to_existing_server(
                server, size_gb, cfvs, datastore=first.disk_storage
            )
    return handler.add_disk_to_existing_server(server, size_gb, cfvs)


def _attach_disks(server, disks):
    handler = server.resource_handler.cast()
    if not getattr(handler, "can_add_disks_after_prov", False):
        raise ValueError(f"{handler.type_slug} resource handlers cannot add disks to a server")
    layout = _layout_for(server, disks)

    for disk in layout:
        if disk.get("attached"):
            set_progress(f"{server.hostname}: disk {disk['disk']} ({_describe(disk)}) is already attached")
            continue
        before = _hypervisor_disks(handler, server)
        set_progress(f"{server.hostname}: adding disk {disk['disk']} ({_describe(disk)})")
        _add_disk(handler, server, disk["size_gb"])
        disk["attached"] = True

        if before is not None:
            after = _hypervisor_disks(handler, server) or {}
            new = [fields for key, fields in after.items() if key not in before]
            if len(new) == 1:
                disk.update(new[0])
                detail = f", VMDK UUID {disk['disk_uuid']}" if disk.get("disk_uuid") else ""
                set_progress(f"{server.hostname}: disk {disk['disk']} is at {_address(disk)}{detail}")
            else:
                set_progress(
                    f"{server.hostname}: {handler.type_slug} shows {len(new)} new disks after adding disk "
                    f"{disk['disk']}; the guest script will match it by attach order and size"
                )
        # Save after every disk so a rerun resumes instead of adding duplicates.
        server.set_value_for_custom_field(LAYOUT_FIELD, json.dumps(layout))
    return layout


def run(job, resource=None, **kwargs):
    try:
        disks = _validate_rows(_parse_rows(DATA_DISKS))
    except (ValueError, SyntaxError) as err:
        return "FAILURE", "", f"Data Disks table: {err}"

    servers = list(resource.server_set.exclude(status="HISTORICAL")) if resource else []
    servers = [s for s in servers if s.is_windows()]
    if not servers:
        return "FAILURE", "", "The resource has no Windows server to attach data disks to."

    _ensure_layout_field()
    summary = []
    for server in servers:
        try:
            layout = _attach_disks(server, disks)
        except Exception as err:
            logger.exception("Attaching data disks to %s failed", server.hostname)
            return "FAILURE", "", f"{server.hostname}: {err}"
        for disk in layout:
            summary.append(f"{server.hostname} disk {disk['disk']}: {_describe(disk)} at {_address(disk)}")

    return "SUCCESS", "\n".join(summary), ""
