"""
CloudBolt teardown plugin: Remove Servers from Ansible Automation Platform.

First teardown step of the "HCP Terraform No-Code Module" blueprint
(BP-00meiwwz, deploy_seq -2): Delete Resource runs the negative-sequence
teardown items in ascending order, so this runs BEFORE the HCP Terraform
teardown (deploy_seq -1) destroys the infrastructure and retires the child
Server records. It removes every host record the resource's servers hold in
Ansible Automation Platform (AAP) inventories -- the records the "Apply
Ansible Automation Configurations to Servers" build step (OHK-cwqouaqn)
created -- so the deployment leaves nothing behind in AAP.

Why a teardown step is needed at all
------------------------------------
CloudBolt removes a server's AAP hosts inside the server decommission job
(jobengine/jobmodules/decomjob.py: delete_server_from_connector), which the
Terraform child records never go through: they are retired (HISTORICAL) by
the HCP teardown because Terraform, not CloudBolt, destroys the VMs. Without
this step the AAP hosts and CloudBolt's AAPHost link rows would outlive the
deployment.

How (platform methods, the decommission job's own sequence)
-----------------------------------------------------------
For each server of the resource that has AAPHost link rows:
  manager = connector_for(server.environment, "install_application")
  manager.delete_server_from_connector(server)
That AAPConf method deletes each linked host through the AAP API and raises
CloudBoltException listing any failure, exactly as during a decommission.
When the environment no longer maps to the manager holding the records (an
environment remapped since the build), the manager that holds them is used
instead. A host AAP already deleted (404) counts as removed. After a
successful removal the server's AAPHost rows are deleted -- the platform only
ever drops them by cascading a server delete, which a retired record never
gets -- so a teardown retry is a clean no-op.

Outcome: the decommission job's convention. A removal failure is a WARNING
with the AAP error ("will still delete it"), never a FAILURE, so an
unreachable AAP cannot block destroying the deployment; the message names
the server and manager so the host can be removed by hand. No resource, or
no server with AAP host records, is nothing to do.

Returns a 3-tuple: (status, output_msg, error_msg).
"""

from common.methods import set_progress
from connectors import connector_for
from connectors.ansible_automation_platform.models import AAPConf
from utilities.exceptions import CloudBoltException
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

ACTION_NAME = "Remove Servers from Ansible Automation Platform"

# The connector feature an Environment's Configuration Management setting maps
# to; the decommission job resolves a server's manager the same way.
INSTALL_FEATURE = "install_application"

# The AAP wrapper raises bare Exceptions whose text carries the HTTP status;
# the AAPConf method joins them into one CloudBoltException. A host that is
# already gone answers 404.
ALREADY_GONE_MARKER = "returned code 404"

STATUS_RANK = {"SUCCESS": 0, "WARNING": 1, "FAILURE": 2}


def _holding_managers(hosts):
    """The distinct AAP managers whose inventories hold these host records."""
    managers = []
    for host in hosts:
        manager = host.inventory.aap
        if manager not in managers:
            managers.append(manager)
    return managers


def _pick_manager(server, holders):
    """
    The manager to remove the server through, and a note when it is not the
    one the server's environment maps to. Mirrors the decommission job: the
    environment's manager acts when it manages the server; otherwise the
    manager actually holding the host records does.
    """
    manager = None
    if server.environment_id is not None:
        manager = connector_for(server.environment, INSTALL_FEATURE)
    if isinstance(manager, AAPConf) and manager.manages_server(server) and manager in holders:
        return manager, ""
    holder = holders[0]
    return holder, (
        "its environment no longer maps to '{}', which holds the host record, "
        "so that manager removed it".format(holder.name)
    )


def _remove_server(server):
    """Remove one server's AAP host records. Returns ``(status, message)``."""
    hostname = server.hostname
    hosts = list(server.aaphosts.select_related("inventory__aap"))
    holders = _holding_managers(hosts)
    manager, note = _pick_manager(server, holders)
    notes = [note] if note else []
    if len(holders) > 1:
        notes.append(
            "host records on several managers ({}); each was deleted through "
            "'{}'".format(", ".join(h.name for h in holders), manager.name)
        )
    host_ids = ", ".join(str(host.id) for host in hosts)

    set_progress(
        "{}: removing AAP host(s) {} through '{}'".format(hostname, host_ids, manager.name)
    )
    logger.info(
        "%s: removing AAP hosts %s via conf %s (id %s) for server id %s",
        hostname, host_ids, manager.name, manager.id, server.id,
    )
    try:
        manager.delete_server_from_connector(server)
    except CloudBoltException as exc:
        text = str(exc)
        # Every failure was a 404: the hosts are already gone in AAP, which
        # is the state this step wants; only the link rows are stale.
        if text.count(ALREADY_GONE_MARKER) >= len(hosts):
            notes.append("AAP had already deleted the host(s)")
        else:
            logger.error("%s: delete_server_from_connector failed: %s", hostname, text)
            return (
                "WARNING",
                "{}: could not remove AAP host(s) {} from '{}'; the records are "
                "kept for a retry. Error: {}".format(
                    hostname, host_ids, manager.name, text
                ),
            )
    except Exception as exc:  # the AAP service/wrapper can raise bare errors
        logger.exception("%s: delete_server_from_connector raised", hostname)
        return (
            "WARNING",
            "{}: could not reach '{}' to remove AAP host(s) {}; the records are "
            "kept for a retry. Error: {}".format(hostname, manager.name, host_ids, exc),
        )

    # The platform drops AAPHost rows only by cascading a server delete, which
    # a retired record never gets; drop them here so a retry has nothing to do.
    server.aaphosts.all().delete()
    message = "{}: removed AAP host(s) {} from '{}'".format(hostname, host_ids, manager.name)
    if notes:
        return "WARNING", "{} ({})".format(message, "; ".join(notes))
    return "SUCCESS", message


def run(job, **kwargs):
    """Remove every AAP host record the resource's servers hold."""
    set_progress("Starting {}...".format(ACTION_NAME))
    logger.info("%s teardown plugin started for job %s", ACTION_NAME, job.id)

    resource = job.resource_set.first()
    if resource is None:
        # Teardown convention: already gone is a WARNING, not a FAILURE.
        return "WARNING", "No resource is associated with this job; nothing to remove.", ""

    # Every record of the deployment, retired ones included: a VM a day-2 run
    # replaced keeps its AAP host until the deployment goes away.
    servers = list(
        resource.server_set.filter(aaphosts__isnull=False)
        .distinct()
        .order_by("hostname", "id")
    )
    if not servers:
        message = (
            "No server of '{}' holds an Ansible Automation Platform host record; "
            "nothing to remove.".format(resource.name)
        )
        set_progress(message)
        return "SUCCESS", message, ""

    outcomes = []
    total = len(servers)
    for index, server in enumerate(servers, start=1):
        if total > 1:
            set_progress("[{}/{}] {}".format(index, total, server.hostname))
        outcomes.append(_remove_server(server))

    worst = max((status for status, _ in outcomes), key=lambda s: STATUS_RANK.get(s, 2))
    if len(outcomes) == 1:
        summary = outcomes[0][1]
    else:
        counts = {key: sum(1 for status, _ in outcomes if status == key) for key in STATUS_RANK}
        summary = "{} server(s): {} removed, {} warning(s).\n{}".format(
            len(outcomes), counts["SUCCESS"], counts["WARNING"],
            "\n".join("- [{}] {}".format(status, message) for status, message in outcomes),
        )
    return worst, summary, ""
