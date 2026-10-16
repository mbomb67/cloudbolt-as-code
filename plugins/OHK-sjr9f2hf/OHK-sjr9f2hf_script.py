"""
Attach Data Disks (Linux)

Build item of the "Oracle Linux Server with Data Disks" blueprint. It runs after
the server tier has built and powered on the VM, and before the in-guest
Configure Data Disks (Oracle Linux) script.

It validates the order form's Data Disks table (mount point, size in GB, volume
name), then, for every Linux server of the resource, adds one disk per row in row
order and saves the rows on the server's data_disk_layout parameter. The
data_disk_mapping shared module does the adding and records, through a provider
per resource handler (vCenter, Azure, Nutanix AHV), what the guest can use to
find each disk: a serial number, a SCSI target, a LUN. Running the job again does
not add disks twice.
"""
import ast
import json
import re

from common.methods import set_progress
from shared_modules import data_disk_mapping as ddm
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The order form's Data Disks table: one dict per row. CloudBolt escapes quotes
# in action inputs before rendering, so the value cannot end the string early.
DATA_DISKS = """{{ data_disks }}"""

MAX_SIZE_GB = 16384
MOUNT_POINT_RE = re.compile(r"^(/[A-Za-z0-9._-]+)+$")
# The volume name becomes vg_<name>, lv_<name>, and the XFS label, which is at
# most 12 characters (xfs_admin(8)).
VOLUME_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,11}$")
# Mounting a data disk over these would hide or break the operating system.
RESERVED_MOUNTS = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root",
    "/run", "/sbin", "/sys", "/tmp", "/usr", "/var",
)
RESERVED_TREES = ("/boot/", "/dev/", "/proc/", "/run/", "/sys/")


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


def _mount_point(value, number):
    path = str(value or "").strip()
    if len(path) > 1:
        path = path.rstrip("/")
    parts = path.split("/")[1:]
    if not MOUNT_POINT_RE.match(path) or any(p in (".", "..") for p in parts):
        raise ValueError(
            f"disk {number}: mount point '{path}' must be an absolute path such as /u01, made of "
            "letters, digits, dots, hyphens and underscores"
        )
    if path in RESERVED_MOUNTS or path.startswith(RESERVED_TREES):
        raise ValueError(f"disk {number}: {path} is an operating system directory and cannot be a mount point")
    return path


def _validate_rows(rows):
    disks, mounts, names = [], [], set()
    for number, row in enumerate(rows, start=1):
        mount = _mount_point(row.get("mount_point"), number)
        name = str(row.get("volume_name") or "").strip()
        size = _size_gb(row.get("size_gb"), number)
        for other in mounts:
            if mount == other or mount.startswith(other + "/") or other.startswith(mount + "/"):
                raise ValueError(f"disk {number}: mount point {mount} overlaps {other}; use separate paths")
        if not VOLUME_NAME_RE.match(name):
            raise ValueError(
                f"disk {number}: volume name '{name}' must be 1 to 12 letters, digits or "
                "underscores, starting with a letter"
            )
        if name.lower() in names:
            raise ValueError(f"disk {number}: volume name '{name}' is already used by another disk")
        mounts.append(mount)
        names.add(name.lower())
        disks.append({"disk": number, "mount_point": mount, "size_gb": size, "volume_name": name})
    return disks


def _describe(disk):
    return f"{disk['mount_point']} {disk['size_gb']} GB '{disk['volume_name']}'"


def run(job, resource=None, **kwargs):
    try:
        disks = _validate_rows(_parse_rows(DATA_DISKS))
    except (ValueError, SyntaxError) as err:
        return "FAILURE", "", f"Data Disks table: {err}"

    servers = list(resource.server_set.exclude(status="HISTORICAL")) if resource else []
    servers = [s for s in servers if not s.is_windows()]
    if not servers:
        return "FAILURE", "", "The resource has no Linux server to attach data disks to."

    ddm.ensure_layout_field()
    summary = []
    for server in servers:
        try:
            layout = ddm.attach_disks(server, disks, _describe)
        except Exception as err:
            logger.exception("Attaching data disks to %s failed", server.hostname)
            return "FAILURE", "", f"{server.hostname}: {err}"
        for disk in layout:
            summary.append(f"{server.hostname} disk {disk['disk']}: {_describe(disk)} at {ddm.address(disk)}")
    set_progress("\n".join(summary))
    return "SUCCESS", "\n".join(summary), ""
