"""
Attach Data Disks (Windows)

Build item of the "Windows Server with Data Disks" blueprint. It runs after the
server tier has built and powered on the VM, and before the in-guest
Configure Data Disks (Windows) script.

It validates the order form's Data Disks table (drive letter, size in GB, volume
name), then, for every Windows server of the resource, adds one disk per row in
row order and saves the rows on the server's data_disk_layout parameter. The
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


def run(job, resource=None, **kwargs):
    try:
        disks = _validate_rows(_parse_rows(DATA_DISKS))
    except (ValueError, SyntaxError) as err:
        return "FAILURE", "", f"Data Disks table: {err}"

    servers = list(resource.server_set.exclude(status="HISTORICAL")) if resource else []
    servers = [s for s in servers if s.is_windows()]
    if not servers:
        return "FAILURE", "", "The resource has no Windows server to attach data disks to."

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
