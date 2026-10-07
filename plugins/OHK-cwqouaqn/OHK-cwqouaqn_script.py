"""
CloudBolt build plugin: Run Ansible Job Templates on Servers.

Second build step of the "HCP Terraform No-Code + Ansible" blueprint
(BP-uh9v24v3, deploy_seq 2). It runs after the no-code module has applied and
the module's VMs have been adopted as child Server records of the resource
(shared module vm_adoption, from the cloudbolt_vm_ids output). For every
server the resource owns it launches the Ansible Automation Platform (AAP)
job templates the blueprint is pinned to, each with a launch payload built
from a declarative "job template map", and waits for the AAP jobs. It is
blueprint-agnostic: any blueprint whose resource ends up owning servers can
add it as a later build step.

No inventory writes
-------------------
CloudBolt's own Ansible Automation Configurations (AAPApplication: inventory
or group + job template) add the host to an inventory before launching. This
step never does: the job templates it launches are bound to inventories that
are managed outside CloudBolt, so it creates no AAP host and no AAPHost row,
and the deployment needs no AAP teardown step. The template is launched as-is
(its own inventory unless the map overrides it) and the host is passed through
the launch's limit and/or extra vars exactly as the map declares.

Job template maps
-----------------
A map is a shared module whose module_name starts with
``ansible_job_template_`` and that defines a module-level dict JOB_TEMPLATE
(shared_modules/SHM-119fepar is the shipped example; the blueprint README
documents every key). The map names the template to launch and says how to
build the payload from CloudBolt data. String values are Django templates
rendered by CloudBolt's own engine (common.methods
generate_string_from_template_for_server, the one behind hostname templates)
with server, resource, blueprint, group, environment, os_build, os_family,
job, order and profile in context, plus every parameter of the server and of
the resource by name. Non-string values (bool, number, list, dict) are passed
through unchanged. Each template declares its own variable names, so two maps
can feed the same CloudBolt fact under different names (one template wants
survey_ip_address, another ip_address); this is why the step builds extra vars
itself instead of using the platform's generate_extra_vars.

A map is loaded fresh on every run (import + reload), so editing a map in the
repo and syncing takes effect without a CloudBolt restart.

Which maps run
--------------
The plugin declares no inputs and the order form carries nothing. The map
module names come from the blueprint parameter ansible_job_template_maps
(STR, multiple values, destination Resource, optional) that the admin pins on
the blueprint's Parameters tab, where "Add option" lists the maps the repo
ships (orchestration_actions/HPA-aualk6ei). The plugin reads, in order:

1. the resource's own values of the parameter: what an order placed through
   CloudBolt's native order form picked (CloudBolt writes Resource-destination
   blueprint parameters onto the resource before build items run);
2. when the resource has none and the blueprint uses a custom form, every
   option pinned on the blueprint. A custom form submits no blueprint-level
   parameters, and CloudBolt copies a parameter onto the resource by itself
   only when it is required with exactly one option, so an optional
   multi-value parameter never reaches the resource on a custom-form order.
   The plugin records the pinned list on the resource so it shows what ran.

No maps anywhere means nothing to run: the step succeeds without touching AAP.

Per server, per map
-------------------
1. Resolve the AAP manager: the map's ``manager`` by name, else
   connector_for(server.environment, "install_application"), the lookup
   CloudBolt's own provisioning hook uses.
2. Find the job template by exact name on that manager (a live AAP query, so
   nothing has to be imported into CloudBolt first).
3. Read the template's launch metadata and render the map.
4. Pre-flight: a survey variable AAP reports as required that the map renders
   empty or does not declare fails the step before anything is launched; a
   limit, inventory or scm_branch the template does not prompt for is dropped
   with a warning (AAP would ignore it anyway).
5. Launch; any field AAP reports as ignored is a warning.
6. Unless the map says otherwise, wait for the AAP job through the platform's
   wait_for_completion_in_aap, which logs its status and stdout.

Outcome per server: SUCCESS when every map ran; WARNING when the server has no
environment, its environment has no AAP manager, a template is not defined on
its manager, or AAP ignored a field; FAILURE when a map cannot be loaded, a
required survey variable is empty, AAP rejected the launch, or a launched job
failed. The job returns the worst outcome. No maps pinned, or a resource with
no servers (the module emitted no cloudbolt_vm_ids), is SUCCESS.

Returns a 3-tuple: (status, output_msg, error_msg).
"""

import importlib
import re
from urllib.parse import quote

from common.methods import (
    add_cfvs_to_context,
    generate_string_from_template_for_server,
    set_progress,
)
from connectors import connector_for
from connectors.ansible_automation_platform.models import AAPConf
from connectors.ansible_automation_platform.service import AAPService
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

ACTION_NAME = "Run Ansible Job Templates on Servers"

# The blueprint parameter (destination Resource, multiple values) that holds
# the map module names; see the module docstring.
MAPS_FIELD = "ansible_job_template_maps"

# Only shared modules named like this are loaded as maps; the options hook
# lists the same set. CloudBolt restricts module names to lowercase letters
# and underscores.
MAP_MODULE_PREFIX = "ansible_job_template_"
MAP_MODULE_PATTERN = re.compile(r"^ansible_job_template_[a-z][a-z_]*$")

# Keys a JOB_TEMPLATE dict may carry; anything else is a typo and fails fast.
MAP_KEYS = {
    "job_template", "manager", "limit", "inventory", "scm_branch",
    "extra_vars", "sensitive", "wait",
}

# The connector feature an Environment's Configuration Management setting maps
# to; connector_for(env, INSTALL_FEATURE) is how the platform's own AAP hooks
# find the manager for a server.
INSTALL_FEATURE = "install_application"

STATUS_RANK = {"SUCCESS": 0, "WARNING": 1, "FAILURE": 2}
MASK = "********"


class MapError(Exception):
    """A job template map is missing, malformed or names things that do not
    exist: configuration, so always a FAILURE."""


# =============================================================================
# == Map names ================================================================
# =============================================================================

def _clean_names(values):
    """Distinct, non-blank names in their original order."""
    names = []
    for value in values or []:
        name = str(value or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _map_names_for(resource):
    """
    The map module names for this deployment and where they came from:
    ``("resource", names)``, ``("blueprint", names)`` or ``("none", [])``.
    """
    on_resource = resource.get_value_for_custom_field(MAPS_FIELD)
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
    pinned = blueprint.custom_field_options.filter(field__name=MAPS_FIELD).order_by("id")
    names = _clean_names(option.value for option in pinned)
    return ("blueprint", names) if names else ("none", [])


def _record_on_resource(resource, names):
    """Store the pinned names on the resource; a failure to do so is logged
    and does not affect the run."""
    try:
        resource.set_value_for_custom_field(MAPS_FIELD, names)
    except Exception:  # noqa: BLE001 -- bookkeeping only
        logger.exception("Could not record %s on resource %s", MAPS_FIELD, resource.id)


# =============================================================================
# == Maps =====================================================================
# =============================================================================

class JobTemplateMap(object):
    """A validated JOB_TEMPLATE dict."""

    def __init__(self, module_name, spec):
        self.module_name = module_name
        self.job_template = str(spec.get("job_template") or "").strip()
        self.manager = str(spec.get("manager") or "").strip()
        self.limit = str(spec.get("limit") or "").strip()
        self.inventory = str(spec.get("inventory") or "").strip()
        self.scm_branch = str(spec.get("scm_branch") or "").strip()
        self.extra_vars = spec.get("extra_vars") or {}
        self.sensitive = {str(key) for key in (spec.get("sensitive") or [])}
        self.wait = bool(spec.get("wait", True))

    def __str__(self):
        return "'{}' ({})".format(self.job_template, self.module_name)


def _load_map(module_name):
    """Import (and reload) the shared module and validate its JOB_TEMPLATE."""
    if not MAP_MODULE_PATTERN.match(module_name):
        raise MapError(
            "'{}' is not a job template map: the shared module's name must "
            "start with '{}' and use lowercase letters and underscores only.".format(
                module_name, MAP_MODULE_PREFIX
            )
        )
    try:
        module = importlib.import_module("shared_modules." + module_name)
        # Shared modules are cached in-process; reload so a synced edit to the
        # map is used without a CloudBolt restart.
        module = importlib.reload(module)
    except Exception as exc:  # ModuleNotFoundError, SyntaxError, anything at import
        raise MapError(
            "job template map '{}' could not be loaded (is the shared module "
            "synced and valid Python?): {}".format(module_name, exc)
        )
    spec = getattr(module, "JOB_TEMPLATE", None)
    if not isinstance(spec, dict):
        raise MapError(
            "job template map '{}' defines no JOB_TEMPLATE dict.".format(module_name)
        )
    unknown = sorted(set(spec) - MAP_KEYS)
    if unknown:
        raise MapError(
            "job template map '{}' has unknown key(s) {}; allowed: {}.".format(
                module_name, ", ".join(unknown), ", ".join(sorted(MAP_KEYS))
            )
        )
    if not isinstance(spec.get("extra_vars") or {}, dict):
        raise MapError(
            "job template map '{}': extra_vars must be a dict of variable name "
            "to value.".format(module_name)
        )
    tmap = JobTemplateMap(module_name, spec)
    if not tmap.job_template:
        raise MapError(
            "job template map '{}': JOB_TEMPLATE['job_template'] (the template's "
            "name in AAP) is required.".format(module_name)
        )
    return tmap


# =============================================================================
# == Rendering ================================================================
# =============================================================================

def _base_context(job, resource):
    """Context shared by every render for this deployment: the job, the
    resource (the renderer adds its blueprint) and the resource's parameters
    by name. The renderer adds the server, its parameters (which win on a name
    clash), group, environment, os_build, order, profile and portal."""
    context = {"job": job, "resource": resource}
    add_cfvs_to_context(resource.attributes.all(), context)
    return context


def _render(value, server, context):
    """Render a map value: strings through CloudBolt's template engine (an
    empty result, or Python's None rendered as text, becomes ""), containers
    recursively, anything else untouched."""
    if isinstance(value, str):
        rendered = generate_string_from_template_for_server(
            value, server, context=dict(context)
        ).strip()
        return "" if rendered == "None" else rendered
    if isinstance(value, dict):
        return {key: _render(item, server, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, server, context) for item in value]
    return value


def _render_extra_vars(tmap, server, context):
    """The launch's extra vars: every map key whose value renders to
    something. A key that renders empty is left out so the template's or the
    survey's default applies (and a required survey variable is caught by the
    pre-flight check)."""
    extra_vars = {}
    for key, value in tmap.extra_vars.items():
        rendered = _render(value, server, context)
        if isinstance(rendered, str) and rendered == "":
            continue
        extra_vars[str(key)] = rendered
    return extra_vars


def _describe(extra_vars, sensitive):
    """``key=value`` pairs for the job log, sensitive values masked."""
    parts = []
    for key in sorted(extra_vars):
        value = MASK if key in sensitive else extra_vars[key]
        parts.append("{}={!r}".format(key, value))
    return ", ".join(parts) or "(none)"


# =============================================================================
# == AAP ======================================================================
# =============================================================================

def _manager_note(manager):
    """How a server's environment is configured, for a skip message."""
    if manager is None:
        return "no configuration manager"
    return "configuration manager '{}' is not an Ansible Automation Platform".format(
        getattr(manager, "name", manager)
    )


def _resolve_manager(server, tmap):
    """The AAPConf to launch through, or ``(None, warning)`` when the server's
    environment gives none. A manager the map names that does not exist is a
    MapError."""
    if tmap.manager:
        manager = AAPConf.objects.filter(name=tmap.manager).first()
        if manager is None:
            raise MapError(
                "job template map {} names manager '{}', which is not an Ansible "
                "Automation Platform configuration manager on this instance.".format(
                    tmap, tmap.manager
                )
            )
        return manager, None

    environment = server.environment
    if environment is None or environment.is_unassigned:
        return None, "no environment, so no Ansible Automation Platform can be resolved"
    manager = connector_for(environment, INSTALL_FEATURE)
    if not isinstance(manager, AAPConf):
        return None, "environment '{}' has {}".format(environment.name, _manager_note(manager))
    return manager, None


def _find_by_name(listing, name):
    """Exact-name matches from an AAP list endpoint's results."""
    data = listing("name={}".format(quote(name, safe="")))
    return [item for item in (data.get("results") or []) if item.get("name") == name]


def _launch(manager, tmap, server, context, job):
    """
    Launch one map's template for one server and return
    ``(status, message, aap_job_id)``; status is SUCCESS, WARNING or FAILURE
    for the launch itself (the wait is the caller's).
    """
    hostname = server.hostname
    service = AAPService.from_conf_id(manager.id)
    wrapper = service.wrapper

    templates = _find_by_name(wrapper.list_job_templates, tmap.job_template)
    if not templates:
        return (
            "WARNING",
            "job template '{}' is not defined on '{}'; skipped.".format(
                tmap.job_template, manager.name
            ),
            None,
        )
    if len(templates) > 1:
        raise MapError(
            "'{}' defines {} job templates named '{}'; the name must be unique.".format(
                manager.name, len(templates), tmap.job_template
            )
        )
    template_id = templates[0]["id"]

    # Launch metadata: what the template prompts for and which survey
    # variables it requires. Automation controller API reference, "Job
    # Templates > Launch" (GET job_templates/{id}/launch/):
    # https://docs.ansible.com/automation-controller/latest/html/controllerapi/api_ref.html
    meta = wrapper.check_job_template_launch(template_id)

    extra_vars = _render_extra_vars(tmap, server, context)
    limit = _render(tmap.limit, server, context)
    scm_branch = _render(tmap.scm_branch, server, context)
    inventory_name = _render(tmap.inventory, server, context)

    notes, errors = [], []
    missing = [
        name for name in (meta.get("variables_needed_to_start") or [])
        if name not in extra_vars
    ]
    if missing:
        errors.append(
            "the template requires survey variable(s) {} that the map renders "
            "empty or does not declare".format(", ".join(missing))
        )
    if meta.get("passwords_needed_to_start"):
        errors.append(
            "the template needs password(s) at launch ({}), which this step "
            "cannot supply".format(", ".join(meta["passwords_needed_to_start"]))
        )
    if meta.get("credential_needed_to_start"):
        errors.append("the template needs a credential chosen at launch")

    inventory_id = None
    if inventory_name:
        if meta.get("ask_inventory_on_launch"):
            inventories = _find_by_name(wrapper.list_inventories, inventory_name)
            if len(inventories) != 1:
                errors.append(
                    "inventory '{}' matches {} inventories on '{}'".format(
                        inventory_name, len(inventories), manager.name
                    )
                )
            else:
                inventory_id = inventories[0]["id"]
        else:
            notes.append("inventory dropped: the template does not prompt for one")
    if meta.get("inventory_needed_to_start") and not inventory_id:
        errors.append("the template has no inventory and the map names none")
    if limit and not meta.get("ask_limit_on_launch"):
        notes.append("limit dropped: the template does not prompt for a limit")
        limit = ""
    if scm_branch and not meta.get("ask_scm_branch_on_launch"):
        notes.append("scm_branch dropped: the template does not prompt for a branch")
        scm_branch = ""
    if extra_vars and not meta.get("ask_variables_on_launch") and not meta.get("survey_enabled"):
        notes.append(
            "the template neither prompts for variables nor has a survey, so "
            "AAP will ignore the extra vars"
        )
    if errors:
        return (
            "FAILURE",
            "{} on '{}' not launched: {}.".format(tmap, manager.name, "; ".join(errors)),
            None,
        )

    set_progress(
        "{}: launching {} on '{}' with extra vars {}{}".format(
            hostname, tmap, manager.name, _describe(extra_vars, tmap.sensitive),
            " and limit '{}'".format(limit) if limit else "",
        )
    )
    # POST job_templates/{id}/launch/ (same reference as above). Fields the
    # template does not prompt for come back under ignored_fields rather than
    # failing the launch; an invalid survey answer is a 400 from the wrapper.
    try:
        response = wrapper.launch_job_template(
            template_id=template_id,
            limit=limit or None,
            extra_vars_override=extra_vars or None,
            scm_branch_override=scm_branch or None,
            inventory_id=inventory_id,
        )
    except Exception as exc:  # the wrapper raises bare requests errors
        logger.exception("%s: launch of %s raised", hostname, tmap)
        return (
            "FAILURE",
            "Ansible Automation Platform '{}' rejected the launch of {}: {}".format(
                manager.name, tmap, exc
            ),
            None,
        )
    aap_job_id = response.get("id")
    if aap_job_id is None:
        return (
            "FAILURE",
            "'{}' answered the launch of {} without a job id: {}".format(
                manager.name, tmap, response
            ),
            None,
        )
    ignored = response.get("ignored_fields") or {}
    if ignored:
        notes.append("AAP ignored {}".format(", ".join(sorted(ignored))))
    message = "launched {} on '{}' as AAP job {}".format(tmap, manager.name, aap_job_id)
    if notes:
        return "WARNING", "{} ({})".format(message, "; ".join(notes)), aap_job_id
    return "SUCCESS", message, aap_job_id


# =============================================================================
# == Per-server work ==========================================================
# =============================================================================

def _configure_server(job, server, maps, context):
    """Run every map for one server. Returns ``(status, message)``."""
    hostname = server.hostname
    outcomes = []
    for tmap in maps:
        try:
            manager, skip = _resolve_manager(server, tmap)
            if manager is None:
                outcomes.append(("WARNING", "{}: {}; skipped.".format(tmap, skip)))
                continue
            status, message, aap_job_id = _launch(manager, tmap, server, context, job)
        except MapError as exc:
            outcomes.append(("FAILURE", str(exc)))
            continue
        if aap_job_id is not None and tmap.wait:
            try:
                wait_status = manager.wait_for_completion_in_aap(job, [aap_job_id])[0]
            except Exception as exc:
                logger.exception("%s: wait_for_completion_in_aap raised", hostname)
                status, message = "FAILURE", (
                    "lost contact with '{}' while waiting for AAP job {}: {}".format(
                        manager.name, aap_job_id, exc
                    )
                )
            else:
                if wait_status != "SUCCESS":
                    status, message = "FAILURE", (
                        "AAP job {} ({}) did not complete successfully; see the "
                        "job log above.".format(aap_job_id, tmap)
                    )
        elif aap_job_id is not None:
            message += " (not waited for)"
        outcomes.append((status, message))

    worst = max((status for status, _ in outcomes), key=lambda s: STATUS_RANK.get(s, 2))
    return worst, "{}: {}".format(hostname, " | ".join(message for _, message in outcomes))


# =============================================================================
# == Entry point ==============================================================
# =============================================================================

def run(job, **kwargs):
    """Launch the pinned job templates for every server of the resource."""
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

    source, names = _map_names_for(resource)
    if not names:
        message = (
            "No Ansible job template maps are set for '{}' (the blueprint "
            "parameter '{}' has no options and the resource carries none); "
            "nothing to run.".format(resource.name, MAPS_FIELD)
        )
        set_progress(message)
        return "SUCCESS", message, ""
    if source == "blueprint":
        set_progress(
            "Using the maps pinned on blueprint '{}': {}".format(
                getattr(resource.blueprint, "name", "?"), ", ".join(names)
            )
        )
        _record_on_resource(resource, names)

    # A bad map is a configuration error for every server: fail before AAP is
    # touched rather than once per server.
    maps = []
    for name in names:
        try:
            maps.append(_load_map(name))
        except MapError as exc:
            return "FAILURE", "", str(exc)

    # Historical records are retired VMs (replaced or destroyed); configuring
    # them is pointless.
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
        "Running {} on {} server(s) of '{}'".format(
            ", ".join(str(tmap) for tmap in maps), len(servers), resource.name
        )
    )
    context = _base_context(job, resource)
    outcomes = []
    total = len(servers)
    for index, server in enumerate(servers, start=1):
        if total > 1:
            set_progress("[{}/{}] {}".format(index, total, server.hostname))
        outcomes.append(_configure_server(job, server, maps, context))

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
