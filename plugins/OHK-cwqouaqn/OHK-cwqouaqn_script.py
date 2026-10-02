"""
CloudBolt build plugin: Apply Ansible Automation Configurations to Servers.

Second build step of the "HCP Terraform No-Code Module" blueprint
(BP-00meiwwz, deploy_seq 2). It runs after the no-code module has applied and
the module's VMs have been adopted as child Server records of the resource
(shared module vm_adoption, from the cloudbolt_vm_ids output), and applies the
Ansible Automation Platform (AAP) configurations predetermined for the
blueprint to every server the resource owns. It is blueprint-agnostic: any
blueprint whose resource ends up owning servers can add it as a later build
step.

Which configurations
--------------------
The plugin declares no inputs and the order form carries nothing. The names
come from the blueprint parameter aap_configuration_names (STR, multiple
values, destination Resource, optional): the admin pins the names as the
parameter's options on the blueprint's Parameters tab, where "Add option"
lists the names defined on the instance's AAP managers (the "Generate options
for Ansible Automation Configurations" action, orchestration_actions/
HPA-aualk6ei). The plugin reads, in order:

1. the resource's own values of aap_configuration_names: what an order placed
   through CloudBolt's native order form picked from the pinned options
   (CloudBolt writes Resource-destination blueprint parameters onto the
   resource before build items run);
2. when the resource has none and the blueprint uses a custom form, every
   option pinned on the blueprint. A custom form replaces the native order
   form completely and submits no blueprint-level parameters, and CloudBolt
   copies a parameter onto the resource on its own only when it is
   "provided" (required, with exactly one option; servicecatalog/forms.py
   get_provided_cfvs inserts an empty choice for an optional one), so an
   optional multi-value parameter never reaches the resource on a
   custom-form order. The pinned options are the admin's predetermined
   list; the plugin records them on the resource so it shows what was
   applied.

No names anywhere means nothing to apply: the step succeeds without touching
AAP.

Matching configurations across configuration managers
-----------------------------------------------------
A CloudBolt AAP configuration (connectors.ansible_automation_platform
AAPApplication) belongs to exactly ONE Ansible Automation Platform
configuration manager (AAPConf), and its name is NOT unique across managers:
the model declares no uniqueness constraint and neither the creation form nor
AAPService.add_application checks for one. "Install Postgres" can therefore be
defined on every AAP manager. The pinned values are configuration NAMES; for
each server the plugin resolves the manager its environment maps to --
connector_for(environment, "install_application"), the very lookup the
out-of-the-box provisioning hook and server action use -- and applies that
manager's configurations with the pinned names. A server in an environment
mapped to AAP East gets East's "Install Postgres"; one mapped to AAP West
gets West's.

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

Outcome per server: SUCCESS when every pinned configuration ran; WARNING when
the server has no environment, its environment has no AAP manager, or a pinned
configuration is not defined on that manager (the others still run); FAILURE
when AAP rejected the host or a launched job failed, canceled or errored. The
job returns the worst outcome. No pinned names, or a resource with no servers
(the module emitted no cloudbolt_vm_ids, or none could be adopted), is
SUCCESS: there is nothing to configure.

Returns a 3-tuple: (status, output_msg, error_msg).
"""

from common.methods import set_progress
from connectors import connector_for
from connectors.ansible_automation_platform.models import AAPApplication, AAPConf
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

ACTION_NAME = "Apply Ansible Automation Configurations to Servers"

# The blueprint parameter (destination Resource, multiple values) that holds
# the configuration names; see the module docstring.
NAMES_FIELD = "aap_configuration_names"

# The connector feature an Environment's Configuration Management setting maps
# to; connector_for(env, INSTALL_FEATURE) is how the platform's own AAP hooks
# find the manager for a server.
INSTALL_FEATURE = "install_application"

STATUS_RANK = {"SUCCESS": 0, "WARNING": 1, "FAILURE": 2}


# =============================================================================
# == Configuration names ======================================================
# =============================================================================

def _clean_names(values):
    """Distinct, non-blank names in their original order."""
    names = []
    for value in values or []:
        name = str(value or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _names_for(resource):
    """
    The configuration names for this deployment and where they came from:
    ``("resource", names)``, ``("blueprint", names)`` or ``("none", [])``.
    """
    on_resource = resource.get_value_for_custom_field(NAMES_FIELD)
    if isinstance(on_resource, str):
        on_resource = [on_resource]
    names = _clean_names(on_resource)
    if names:
        return "resource", names

    # Only a custom-form order can have pinned options that never reached the
    # resource. On the native order form the orderer saw them and picked
    # (or picked none), and the resource is the answer.
    blueprint = getattr(resource, "blueprint", None)
    if blueprint is None or not getattr(blueprint, "custom_form", None):
        return "none", []
    pinned = blueprint.custom_field_options.filter(field__name=NAMES_FIELD).order_by("id")
    names = _clean_names(option.value for option in pinned)
    return ("blueprint", names) if names else ("none", [])


def _record_on_resource(resource, names):
    """Store the pinned names on the resource; a failure to do so is logged
    and does not affect the apply."""
    try:
        resource.set_value_for_custom_field(NAMES_FIELD, names)
    except Exception:  # noqa: BLE001 -- bookkeeping only
        logger.exception(
            "Could not record %s on resource %s", NAMES_FIELD, resource.id
        )


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

    # One configuration per pinned name: names are not unique even within a
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
            "{}: none of the pinned configurations are defined on '{}' "
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
    """Apply the pinned AAP configurations to every server of the resource."""
    set_progress("Starting {}...".format(ACTION_NAME))
    logger.info("%s build plugin started for job %s", ACTION_NAME, job.id)

    resource = job.resource_set.first()
    if resource is None:
        return (
            "FAILURE",
            "",
            "No resource is associated with this job. This plugin runs as a "
            "build step of a blueprint whose earlier steps create the "
            "resource's servers.",
        )

    source, names = _names_for(resource)
    if not names:
        message = (
            "No Ansible Automation Configurations are set for '{}' (the "
            "blueprint parameter '{}' has no options and the resource carries "
            "none); nothing to apply.".format(resource.name, NAMES_FIELD)
        )
        set_progress(message)
        return "SUCCESS", message, ""
    if source == "blueprint":
        set_progress(
            "Using the configurations pinned on blueprint '{}': {}".format(
                getattr(resource.blueprint, "name", "?"), ", ".join(names)
            )
        )
        _record_on_resource(resource, names)

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
