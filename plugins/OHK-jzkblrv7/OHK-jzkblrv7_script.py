"""
Attach Data Disks from Disk Map

Post-Provision orchestration action. For every server of the job with a disk_map
parameter, it validates the map for the server's operating system (drive letters
on Windows, mount points on Linux), adds one disk per entry in order through the
server's resource handler, and saves on data_disk_layout the SCSI controller,
target, LUN, and serial the hypervisor gave each disk. The data_disk_mapping
shared module does the adding through a provider per resource handler (vCenter,
Azure, Nutanix AHV). Servers without a disk_map are skipped. Running the job
again does not add disks twice.

The Configure Data Disks from Disk Map actions run next and prepare the disks
inside the guest.
"""
from common.methods import set_progress
from shared_modules import data_disk_mapping as ddm
from shared_modules import disk_map as dm
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def run(job, *args, **kwargs):
    # Post-Provision passes no server kwarg; the job's servers are the ones built.
    servers = [s for s in job.server_set.all() if dm.raw_value(s)]
    if not servers:
        set_progress("No server in this job has a disk_map.")
        return "SUCCESS", "", ""

    ddm.ensure_layout_field()
    summary, failures = [], []
    for server in servers:
        try:
            disks = dm.disks_for(server)
        except ValueError as err:
            failures.append(f"{server.hostname}: disk_map: {err}")
            continue
        try:
            layout = ddm.attach_disks(server, disks, dm.describe)
        except Exception as err:
            logger.exception("Attaching data disks to %s failed", server.hostname)
            failures.append(f"{server.hostname}: {err}")
            continue
        for disk in layout:
            summary.append(f"{server.hostname} disk {disk['disk']}: {dm.describe(disk)} at {ddm.address(disk)}")

    if summary:
        set_progress("\n".join(summary))
    if failures:
        return "FAILURE", "", "\n".join(failures)
    return "SUCCESS", "", ""
