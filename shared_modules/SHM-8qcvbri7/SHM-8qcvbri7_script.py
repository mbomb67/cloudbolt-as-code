"""
Disk map: the disk_map server parameter and the orchestration steps that act on it.

disk_map is a TXT parameter holding a JSON list of disks, one dict per disk:

    drive_letter  Windows drive letter, D to Z.
    mount_point   Linux mount point, an absolute path such as /u01.
    size_gb       Whole GB, 1 to 16384.
    volume_name   NTFS label (Windows, up to 32 characters) or, on Linux, the
                  vg_<name>/lv_<name> names and the XFS label (up to 12 letters,
                  digits or underscores, starting with a letter).

A row may carry both a drive letter and a mount point; the server's operating
system picks the one that applies. Usually disk_map is an environment parameter,
filled in by the order form's Data Disks table (form function dataDiskMapForm),
but any source that sets it on the server works: an API order, another blueprint,
a native order form.

Three Post-Provision orchestration actions use this module, in run_seq order:
  Attach Data Disks from Disk Map      validates disk_map for the server's OS and
                                       adds the disks through data_disk_mapping,
                                       which saves data_disk_layout.
  Configure Data Disks from Disk Map   one per OS family; runs the in-guest script
  (Windows), (Oracle Linux)            with data_disk_layout filled in.
Servers without a disk_map are skipped by all three, so the actions can stay
enabled for every provisioning job.
"""
import ast
import html
import json
import re

from common.methods import set_progress
from infrastructure.models import CustomField
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

FIELD = "disk_map"
LAYOUT_FIELD = "data_disk_layout"  # written by data_disk_mapping.attach_disks
LAYOUT_TOKEN = "__CB_DATA_DISK_LAYOUT__"  # replaced in the guest scripts
GUEST_TIMEOUT = 900

MAX_SIZE_GB = 16384  # NTFS limit with the default 4 KB cluster size
DRIVE_LETTER_RE = re.compile(r"^[D-Z]$")  # A and B are legacy floppy letters, C is the OS
WINDOWS_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,31}$")  # NTFS labels: 32 characters
MOUNT_POINT_RE = re.compile(r"^(/[A-Za-z0-9._-]+)+$")
# The Linux volume name becomes vg_<name>, lv_<name>, and the XFS label, which is
# at most 12 characters (xfs_admin(8)).
LINUX_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,11}$")
# Mounting a data disk over these would hide or break the operating system.
RESERVED_MOUNTS = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root",
    "/run", "/sbin", "/sys", "/tmp", "/usr", "/var",
)
RESERVED_TREES = ("/boot/", "/dev/", "/proc/", "/run/", "/sys/")


def ensure_field():
    CustomField.objects.get_or_create(
        name=FIELD,
        defaults=dict(
            label="Data Disks",
            type="TXT",
            show_on_servers=True,
            description=(
                "Data disks to add to the server, as a JSON list: one entry per disk with "
                "drive_letter (Windows) or mount_point (Linux), size_gb, and volume_name."
            ),
        ),
    )


def raw_value(server):
    return str(server.get_value_for_custom_field(FIELD) or "").strip()


def parse(raw):
    """Return disk_map as a list of dicts. Raise ValueError when it is not one."""
    # API orders store text HTML-escaped (&quot;); a table submitted as a list is
    # stored as its Python literal.
    raw = html.unescape(raw or "").strip()
    if not raw:
        raise ValueError("it lists no disks")
    try:
        rows = json.loads(raw)
    except ValueError:
        try:
            rows = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            raise ValueError("it is not a JSON list of disks")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        raise ValueError("expected a list of disks, one dict per disk")
    return rows


def _size_gb(value, number):
    try:
        size = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"disk {number}: size '{value}' is not a number")
    if size != int(size) or not 1 <= size <= MAX_SIZE_GB:
        raise ValueError(f"disk {number}: size must be a whole number of GB from 1 to {MAX_SIZE_GB}")
    return int(size)


def windows_disks(rows):
    disks, letters = [], set()
    for number, row in enumerate(rows, start=1):
        letter = str(row.get("drive_letter") or "").strip().rstrip(":").upper()
        name = str(row.get("volume_name") or "").strip()
        size = _size_gb(row.get("size_gb"), number)
        if not letter:
            raise ValueError(f"disk {number}: a Windows server needs a drive_letter")
        if not DRIVE_LETTER_RE.match(letter):
            raise ValueError(f"disk {number}: drive letter '{letter}' must be one of D to Z")
        if letter in letters:
            raise ValueError(f"disk {number}: drive letter {letter}: is already used by another disk")
        if not WINDOWS_NAME_RE.match(name):
            raise ValueError(
                f"disk {number}: volume name '{name}' must be 1 to 32 letters, digits, spaces, "
                "hyphens or underscores, starting with a letter or digit"
            )
        letters.add(letter)
        disks.append({"disk": number, "drive_letter": letter, "size_gb": size, "volume_name": name})
    return disks


def _mount_point(value, number):
    path = str(value or "").strip()
    if not path:
        raise ValueError(f"disk {number}: a Linux server needs a mount_point")
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


def linux_disks(rows):
    disks, mounts, names = [], [], set()
    for number, row in enumerate(rows, start=1):
        mount = _mount_point(row.get("mount_point"), number)
        name = str(row.get("volume_name") or "").strip()
        size = _size_gb(row.get("size_gb"), number)
        for other in mounts:
            if mount == other or mount.startswith(other + "/") or other.startswith(mount + "/"):
                raise ValueError(f"disk {number}: mount point {mount} overlaps {other}; use separate paths")
        if not LINUX_NAME_RE.match(name):
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


def disks_for(server):
    """The server's disk_map, validated for its operating system."""
    rows = parse(raw_value(server))
    return windows_disks(rows) if server.is_windows() else linux_disks(rows)


def describe(disk):
    target = f"{disk['drive_letter']}:" if "drive_letter" in disk else disk["mount_point"]
    return f"{target} {disk['size_gb']} GB '{disk['volume_name']}'"


def _guest_output(err):
    return str(getattr(err, "output", "") or err).strip()


def configure_in_guest(job, servers, script, file_extension, run_with_sudo):
    """
    Run an in-guest script on every server that has a disk_map, with the server's
    data_disk_layout in place of LAYOUT_TOKEN. Returns CloudBolt's
    (status, output, error) tuple.

    The script runs through Server.execute_script, the call a Remote Script action
    makes (cbhooks RemoteScriptHook.run_hook_on_server): the resource handler's own
    channel when tech-specific script execution is on (VMware Tools, Azure Run
    Command), else WinRM or SSH with the server's credentials.
    """
    done, skipped, failures = [], [], []
    for server in servers:
        if not raw_value(server):
            skipped.append(f"{server.hostname}: no disk_map")
            continue
        layout = str(server.get_value_for_custom_field(LAYOUT_FIELD) or "").strip()
        try:
            layout = json.dumps(json.loads(layout), separators=(",", ":")) if layout else ""
        except ValueError:
            layout = ""
        if not layout:
            failures.append(
                f"{server.hostname}: disk_map is set but {LAYOUT_FIELD} is not. Attach Data Disks "
                "from Disk Map must run first at Post-Provision (enabled, lower run_seq)."
            )
            continue

        body = script.replace(LAYOUT_TOKEN, layout)
        if file_extension == "sh":
            body = body.replace("\r\n", "\n")  # what a Remote Script action does for Linux
        set_progress(f"{server.hostname}: configuring data disks in the guest")
        try:
            output = server.execute_script(
                script_contents=body,
                file_extension=file_extension,
                run_with_sudo=run_with_sudo,
                timeout=GUEST_TIMEOUT,
            )
        except Exception as err:
            logger.exception("Configuring data disks on %s failed", server.hostname)
            failures.append(f"{server.hostname}: {_guest_output(err)}")
            continue
        set_progress(f"{server.hostname}:\n{output}")
        done.append(f"{server.hostname}: data disks configured")

    if failures:
        return "FAILURE", "\n".join(done + skipped), "\n".join(failures)
    if not done:
        return "SUCCESS", "No server in this job has a disk_map.", ""
    return "SUCCESS", "\n".join(done + skipped), ""
