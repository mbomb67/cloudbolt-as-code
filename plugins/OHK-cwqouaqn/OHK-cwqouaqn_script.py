"""
CloudBolt build plugin: Apply Ansible Automation Configurations to Servers.

Second build step of the "HCP Terraform No-Code Module" blueprint
(BP-00meiwwz, deploy_seq 2). It runs after the no-code module has applied and
the module's VMs have been adopted as child Server records of the resource
(shared module vm_adoption, from the cloudbolt_vm_ids output), and applies the
Ansible Automation Platform (AAP) configurations the orderer picked to every
server the resource owns. It is blueprint-agnostic: any blueprint whose
resource ends up owning servers can add it as a later build step.

Matching configurations across configuration managers
-----------------------------------------------------
A CloudBolt AAP configuration (connectors.ansible_automation_platform
AAPApplication) belongs to exactly ONE Ansible Automation Platform
configuration manager (AAPConf), and its name is NOT unique across managers:
the model declares no uniqueness constraint and neither the creation form nor
AAPService.add_application checks for one. "Install Postgres" can therefore be
defined on every AAP manager. The orderer picks configuration NAMES (one
multi-select, deduplicated across the managers mapped to the environments the
ordering group may use); for each server the plugin resolves the manager its
environment maps to -- connector_for(environment, "install_application"), the
very lookup the out-of-the-box provisioning hook and server action use -- and
applies that manager's configurations with the chosen names. A server in an
environment mapped to AAP East gets East's "Install Postgres"; one mapped to
AAP West gets West's.

Applying
--------
Platform methods only, in the same sequence as the out-of-the-box hooks
(cbhooks/hookmodules/apply_ansible_automation_configurations.py and
ansible_automation_with_action_input.py): AAPConf.apply_configurations_in_aap
adds the host to each configuration's inventory or group and launches its job
template or workflow with CloudBolt's extra vars (hostname, IP, CPU, memory and
the server's parameters); AAPConf.wait_for_completion_in_aap polls the launched
AAP jobs to completion and logs their stdout. Servers are processed one after
another; a failure on one does not stop the others.

Input
-----
aap_configuration_names (STR, multi-select): the configuration names to
apply. Optional -- nothing selected means nothing to do. Options come from
generate_options_for_aap_configuration_names, scoped by the ordering group's
entitled environments (group.get_available_environments(), cardinal rule 4).

Outcome per server: SUCCESS when every chosen configuration ran; WARNING when
the server has no environment, its environment has no AAP manager, or a chosen
configuration is not defined on that manager (the others still run); FAILURE
when AAP rejected the host or a launched job failed, canceled or errored. The
job returns the worst outcome. A resource with no servers (the module emitted
no cloudbolt_vm_ids, or none could be adopted) is SUCCESS: there is nothing
to configure.

Returns a 3-tuple: (status, output_msg, error_msg).
"""

import ast

from common.methods import set_progress
from connectors import connector_for
from connectors.ansible_automation_platform.models import AAPApplication, AAPConf
from connectors.models import FeatureMap
from utilities.logger import ThreadLogger

from shared_modules.env_options import resolve_group

logger = ThreadLogger(__name__)

ACTION_NAME = "Apply Ansible Automation Configurations to Servers"

# The connector feature an Environment's Configuration Management setting maps
# to; connector_for(env, INSTALL_FEATURE) is how the platform's own AAP hooks
# find the manager for a server.
INSTALL_FEATURE = "install_application"

STATUS_RANK = {"SUCCESS": 0, "WARNING": 1, "FAILURE": 2}


# =============================================================================
# == Options ==================================================================
# =============================================================================

def _managers_for_group(group):
    """
    The AAP configuration managers mapped (Configuration Management feature)
    to any environment the group may order into. None when no group is in
    context (e.g. the blueprint editor's preview), meaning "every manager".
    """
    if group is None:
        return None
    # Cardinal rule 4: the platform's entitlement query (explicit grants, the
    # ancestors' grants, and unconstrained environments), never group__in.
    environments = list(group.get_available_environments())
    if not environments:
        return AAPConf.objects.none()
    conf_ids = FeatureMap.objects.filter(
        feature__name=INSTALL_FEATURE, environment__in=environments
    ).values_list("connector_conf_id", flat=True)
    # AAPConf extends ConnectorConf by multi-table inheritance, so the ids
    # match; non-AAP managers (Chef, Puppet, ...) simply do not join.
    return AAPConf.objects.filter(id__in=list(conf_ids))


def generate_options_for_aap_configuration_names(field=None, **kwargs):
    """
    Configuration names available to the ordering group, one option per
    distinct name. The label lists the managers that define the name, so an
    orderer can see "Install Postgres (AAP East, AAP West)" is served
    wherever the deployment's servers land.
    """
    group = resolve_group(kwargs.get("group"))
    managers = _managers_for_group(group)
    apps = AAPApplication.objects.all()
    if managers is not None:
        apps = apps.filter(aap__in=managers)
    by_name = {}
    for name, manager_name in apps.values_list("name", "aap__name").order_by(
        "name", "aap__name"
    ):
        defined_on = by_name.setdefault(name, [])
        if manager_name not in defined_on:
            defined_on.append(manager_name)
    return [
        (name, "{} ({})".format(name, ", ".join(defined_on)))
        for name, defined_on in by_name.items()
    ]


# =============================================================================
# == Input parsing ============================================================
# =============================================================================

def _parse_names(rendered):
    """
    The chosen names from the rendered multi-select input: CloudBolt renders a
    multi-value input as the Python repr of a list of strings (nothing chosen
    renders empty). A value that is not a literal is taken as one name.
    Blank entries and duplicates are dropped; order is preserved.
    """
    text = (rendered or "").strip()
    if not text or text in ("None", "[]"):
        return []
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        value = text
    items = list(value) if isinstance(value, (list, tuple, set)) else [value]
    names = []
    for item in items:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names


# =============================================================================
# == Per-server work ==========================================================
# =============================================================================

def _manager_note(manager):
    """How a server's environment is configured, for a skip message."""
    if manager is None:
        return "no configuration manager"
    return "configuration manager '{}' is not an Ansible Automation Platform".format(
        getattr(manager, "name", manager)
    )


def _configure_server(job, server, names):
    """
    Apply the named configurations to one server through the AAP manager its
    environment maps to. Returns ``(status, message)``.
    """
    hostname = server.hostname
    environment = server.environment
    if environment is None or environment.is_unassigned:
        return (
            "WARNING",
            "{}: no environment, so no Ansible Automation Platform can be "
            "resolved for it; skipped.".format(hostname),
        )

    manager = connector_for(environment, INSTALL_FEATURE)
    if not isinstance(manager, AAPConf):
        return (
            "WARNING",
            "{}: environment '{}' has {}; skipped.".format(
                hostname, environment.name, _manager_note(manager)
            ),
        )

    # One configuration per chosen name: names are not unique even within a
    # manager, so a duplicate definition is applied once (the oldest) and
    # reported rather than run twice.
    apps, duplicates = [], []
    for app in AAPApplication.objects.filter(aap=manager, name__in=names).order_by(
        "name", "id"
    ):
        if any(existing.name == app.name for existing in apps):
            duplicates.append(app.name)
        else:
            apps.append(app)
    found = {app.name for app in apps}
    missing = [name for name in names if name not in found]
    notes = []
    if missing:
        notes.append(
            "not defined on '{}': {}".format(manager.name, ", ".join(missing))
        )
    if duplicates:
        notes.append(
            "defined more than once on '{}' (applied once): {}".format(
                manager.name, ", ".join(sorted(set(duplicates)))
            )
        )
    if not apps:
        return (
            "WARNING",
            "{}: none of the chosen configurations are defined on '{}' "
            "({}); skipped.".format(hostname, manager.name, "; ".join(notes)),
        )

    applied = ", ".join(app.name for app in apps)
    set_progress(
        "{}: applying {} through '{}'".format(hostname, applied, manager.name)
    )
    logger.info(
        "%s: applying AAP configurations %s via conf %s (id %s) for server id %s",
        hostname, applied, manager.name, manager.id, server.id,
    )

    # The platform method adds the host to each configuration's inventory or
    # group and launches its job; it reports its own progress and answers
    # FAILURE (with the ids launched so far) when AAP rejects a step.
    try:
        status, aap_job_ids = manager.apply_configurations_in_aap(server, apps, job)
    except Exception as exc:  # the AAP wrapper raises bare requests errors
        logger.exception("%s: apply_configurations_in_aap raised", hostname)
        return (
            "FAILURE",
            "{}: Ansible Automation Platform '{}' could not be reached or "
            "rejected the host: {}".format(hostname, manager.name, exc),
        )
    if status != "SUCCESS":
        return (
            "FAILURE",
            "{}: Ansible Automation Platform '{}' rejected the host or a job "
            "launch; see the job log above.".format(hostname, manager.name),
        )

    if aap_job_ids:
        try:
            status = manager.wait_for_completion_in_aap(job, aap_job_ids)[0]
        except Exception as exc:
            logger.exception("%s: wait_for_completion_in_aap raised", hostname)
            return (
                "FAILURE",
                "{}: lost contact with '{}' while waiting for AAP job(s) {}: "
                "{}".format(hostname, manager.name, aap_job_ids, exc),
            )
        if status != "SUCCESS":
            return (
                "FAILURE",
                "{}: an AAP job did not complete successfully (launched {}); "
                "see the job log above.".format(hostname, aap_job_ids),
            )

    message = "{}: applied {} through '{}' (AAP job(s) {})".format(
        hostname, applied, manager.name, ", ".join(str(i) for i in aap_job_ids) or "none"
    )
    if notes:
        return "WARNING", "{}; {}".format(message, "; ".join(notes))
    return "SUCCESS", message


# =============================================================================
# == Entry point ==============================================================
# =============================================================================

def run(job, **kwargs):
    """Apply the chosen AAP configurations to every server of the resource."""
    set_progress("Starting {}...".format(ACTION_NAME))
    logger.info("%s build plugin started for job %s", ACTION_NAME, job.id)

    # Cardinal rule 3: the multi-select renders as a list repr, so it is
    # triple-quoted and parsed rather than used raw.
    names = _parse_names("""{{ aap_configuration_names }}""")

    resource = job.resource_set.first()
    if resource is None:
        return (
            "FAILURE",
            "",
            "No resource is associated with this job. This plugin runs as a "
            "build step of a blueprint whose earlier steps create the "
            "resource's servers.",
        )

    # Historical records are retired VMs (replaced or destroyed); configuring
    # them is pointless and AAP would fail on the stale host.
    servers = list(
        resource.server_set.exclude(status="HISTORICAL").order_by("hostname", "id")
    )
    if not servers:
        message = (
            "Resource '{}' has no servers to configure (the template emitted no "
            "cloudbolt_vm_ids, or no VM could be adopted); nothing to do.".format(
                resource.name
            )
        )
        set_progress(message)
        return "SUCCESS", message, ""
    if not names:
        message = (
            "No Ansible Automation Configurations were chosen; nothing to apply "
            "to the {} server(s) of '{}'.".format(len(servers), resource.name)
        )
        set_progress(message)
        return "SUCCESS", message, ""

    set_progress(
        "Applying {} to {} server(s) of '{}'".format(
            ", ".join(names), len(servers), resource.name
        )
    )
    outcomes = []
    total = len(servers)
    for index, server in enumerate(servers, start=1):
        if total > 1:
            set_progress("[{}/{}] {}".format(index, total, server.hostname))
        outcomes.append(_configure_server(job, server, names))

    worst = max((status for status, _ in outcomes), key=lambda s: STATUS_RANK.get(s, 2))
    if len(outcomes) == 1:
        summary = outcomes[0][1]
    else:
        counts = {key: sum(1 for status, _ in outcomes if status == key) for key in STATUS_RANK}
        summary = "{} server(s): {} configured, {} warning(s), {} failed.\n{}".format(
            len(outcomes), counts["SUCCESS"], counts["WARNING"], counts["FAILURE"],
            "\n".join("- [{}] {}".format(status, message) for status, message in outcomes),
        )
    if worst == "FAILURE":
        return "FAILURE", "", summary
    return worst, summary, ""
