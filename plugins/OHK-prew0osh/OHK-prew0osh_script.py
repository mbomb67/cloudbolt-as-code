"""
CloudBolt build plugin: OpenShift Project Landing Zone.

Creates a governed OpenShift project for a team and wires it into CloudBolt so
the team can immediately order the existing VM and container blueprints into
it -- but only into it.

On the cluster (via the selected environment's OpenShift Virtualization handler):
  1. Namespace with OpenShift project annotations and CloudBolt ownership labels
  2. ResourceQuota sized by the chosen tier
  3. LimitRange with default container requests/limits (required with a quota)
  4. Optional multitenant-isolation NetworkPolicies
  5. Optional RoleBinding granting an OpenShift group the admin role

In CloudBolt:
  6. An Environment pinned to the new namespace
  7. The ordering Group -- and optionally one more -- entitled to that Environment
  8. The tier mirrored into the Environment quota

Expected Action Inputs (declared in OHK-prew0osh_metadata.json):
  - env_id (INT, required)             : OpenShift Virtualization environment (RBAC gate)
  - project_name (STR, required)       : DNS-1123 namespace name
  - display_name (STR, optional)
  - size_tier (STR, required)          : small | medium | large | xlarge
  - additional_group_id (INT, optional): extra CloudBolt Group to entitle
  - team_group_name (STR, optional)    : OpenShift group (listed from the cluster)
  - network_isolation (BOOL, optional) : apply isolation NetworkPolicies

Expiration comes from the blueprint's standard expiration_date parameter, which
CloudBolt stores on the Resource before this plugin runs; it is read from there
and mirrored onto the namespace as an annotation.

RBAC: end users pick an Environment; the resource handler is derived from it
inside run() and never shown on the order form (docs/agents/rbac-and-security.md).

Returns a 3-tuple: (status, output_msg, error_msg)
"""

from accounts.models import Group
from common.methods import set_progress
from infrastructure.models import Environment
from resourcehandlers.ovirt.models import OVirtHandler
from shared_modules.openshift_landing_zone import (
    ANNOTATION_ENVIRONMENT,
    ANNOTATION_EXPIRES,
    ANNOTATION_OS_DESCRIPTION,
    ANNOTATION_OS_DISPLAY_NAME,
    ANNOTATION_OS_REQUESTER,
    ANNOTATION_OWNER,
    CF_PREFIX,
    LABEL_GROUP,
    LABEL_RESOURCE_ID,
    LABEL_TIER,
    LANDING_ZONE_LABEL,
    LandingZoneError,
    OpenShiftLandingZoneClient,
    ensure_custom_fields,
    ensure_environment_for_namespace,
    entitle_group,
    entitled_group_names,
    get_tier,
    label_value,
    quota_hard_for_tier,
    render_bindings,
    resource_expiration,
    set_environment_quota,
    store_tier_on_resource,
    tier_options,
    validate_namespace_name,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


# ---------------------------------------------------------------------------
# Order-form option generators
# ---------------------------------------------------------------------------

def _resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def _handler_from_env_id(env_id):
    """Return the cast OVirtHandler behind an environment id, or None."""
    try:
        env = Environment.objects.get(id=int(env_id))
    except (Environment.DoesNotExist, TypeError, ValueError):
        return None
    rh = env.resource_handler.cast() if env.resource_handler else None
    return rh if isinstance(rh, OVirtHandler) else None


def generate_options_for_env_id(field, **kwargs):
    """RBAC-aware Environment selector, restricted to OpenShift-backed environments.

    Standard: Group.get_available_environments() returns the environments
    explicitly entitled to the requesting group (and its ancestors) PLUS any
    unconstrained environments (no groups assigned). Users must be able to
    order into both, so never filter Environment by group__in alone.
    See docs/agents/rbac-and-security.md.
    """
    group = _resolve_group(kwargs.get("group"))
    if not group:
        return []

    available_ids = [env.id for env in group.get_available_environments()]
    envs = Environment.objects.filter(
        id__in=available_ids,
        resource_handler__ovirthandler__isnull=False,
    ).order_by("name")
    if not envs.exists():
        return [("", "------ No OpenShift environments available ------")]

    return [(env.id, env.name) for env in envs]


def generate_options_for_size_tier(field, **kwargs):
    """Tier dropdown; smallest tier preselected."""
    return tier_options(initial="small")


def generate_options_for_additional_group_id(field, **kwargs):
    """Every real CloudBolt Group; the ordering group is entitled regardless."""
    groups = Group.objects.exclude(name__startswith="Unassigned").order_by("name")
    options = [("", "------ None (ordering group only) ------")]
    options.extend((g.id, g.name) for g in groups)
    return {"options": options, "sort": False}


def generate_options_for_team_group_name(field, control_value=None, **kwargs):
    """OpenShift groups from the selected cluster.

    control_value is the chosen env_id (REGENOPTIONS dependency). When it has
    not arrived -- no cluster picked yet, or the dependency was not imported --
    fall back to the groups of every OpenShift Virtualization handler so the
    field is still usable; with more than one cluster the labels are prefixed
    with the cluster name.
    """
    none_option = ("", "------ None (manage access through CloudBolt only) ------")

    rh = _handler_from_env_id(control_value) if control_value else None
    handlers = [rh] if rh is not None else list(OVirtHandler.objects.all())
    if not handlers:
        return [none_option]

    options = [none_option]
    prefix = len(handlers) > 1
    for handler in handlers:
        try:
            groups = OpenShiftLandingZoneClient.from_handler(handler).list_groups()
        except Exception as exc:
            logger.warning("Could not list OpenShift groups for handler %s: %s", handler, exc)
            options.append(("", f"------ {handler.name}: could not list groups ------"))
            continue
        options.extend(
            (name, f"{handler.name}: {name}" if prefix else name) for name in groups
        )
    return {"options": options, "sort": False}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _owner_username(job):
    owner = getattr(job, "owner", None)
    user = getattr(owner, "user", None)
    return getattr(user, "username", "") or str(owner or "")


def run(job, **kwargs):
    """Create the OpenShift project landing zone."""
    set_progress("Starting OpenShift Project Landing Zone build...")
    logger.info("Landing zone build plugin started for job %s", job.id)

    ensure_custom_fields()

    env_id_str = "{{ env_id }}".strip()
    project_name = "{{ project_name }}".strip().lower()
    display_name = "{{ display_name }}".strip() or project_name
    tier_key = "{{ size_tier }}".strip().lower()
    additional_group_str = "{{ additional_group_id }}".strip()
    team_group_name = "{{ team_group_name }}".strip()
    network_isolation = "{{ network_isolation }}".strip().lower() in ("true", "1", "yes", "on")

    # ---- Validate inputs ------------------------------------------------
    if not env_id_str:
        return "FAILURE", "", "An OpenShift cluster (environment) is required."
    try:
        env_id = int(env_id_str)
    except ValueError:
        return "FAILURE", "", f"Invalid env_id value '{env_id_str}'."

    name_error = validate_namespace_name(project_name)
    if name_error:
        return "FAILURE", "", name_error

    try:
        tier = get_tier(tier_key)
    except ValueError as exc:
        return "FAILURE", "", str(exc)

    # ---- Resolve handler from environment (RBAC gate) -------------------
    try:
        source_env = Environment.objects.get(id=env_id)
    except Environment.DoesNotExist:
        return "FAILURE", "", f"Environment with id={env_id} not found."

    rh = source_env.resource_handler.cast()
    if not isinstance(rh, OVirtHandler):
        return "FAILURE", "", "Selected environment is not backed by an OpenShift Virtualization handler."

    resource = job.resource_set.first()
    ordering_group = getattr(resource, "group", None)
    owner = _owner_username(job)
    expires = resource_expiration(resource)

    additional_group = None
    if additional_group_str:
        additional_group = Group.objects.filter(id=int(additional_group_str)).first()
        if additional_group is None:
            return "FAILURE", "", f"Additional group id={additional_group_str} not found."

    try:
        client = OpenShiftLandingZoneClient.from_handler(rh)
    except Exception as exc:
        logger.exception("Could not connect to OpenShift API")
        return "FAILURE", "", f"Could not connect to the OpenShift API for handler '{rh.name}': {exc}"

    # ---- Refuse to adopt an existing namespace --------------------------
    try:
        if client.get_namespace(project_name) is not None:
            return (
                "FAILURE",
                "",
                f"A project named '{project_name}' already exists on '{rh.name}'. Choose a "
                "different name, or import it with the blueprint's discovery plugin.",
            )
    except LandingZoneError as exc:
        return "FAILURE", "", f"Could not check for an existing project: {exc}"

    # ---- 1. Namespace ---------------------------------------------------
    labels = {
        LANDING_ZONE_LABEL: "true",
        LABEL_TIER: tier_key,
    }
    if ordering_group is not None:
        labels[LABEL_GROUP] = label_value(ordering_group.name)
    if resource is not None:
        labels[LABEL_RESOURCE_ID] = label_value(resource.global_id)

    annotations = {
        ANNOTATION_OS_DISPLAY_NAME: display_name,
        ANNOTATION_OS_DESCRIPTION: (
            f"Landing zone provisioned by CloudBolt for group "
            f"'{getattr(ordering_group, 'name', 'n/a')}' (tier: {tier['label']})."
        ),
        ANNOTATION_OWNER: owner,
    }
    if owner:
        annotations[ANNOTATION_OS_REQUESTER] = owner
    if expires:
        annotations[ANNOTATION_EXPIRES] = expires.isoformat()

    set_progress(f"Creating OpenShift project '{project_name}' on '{rh.name}'...")
    try:
        namespace = client.create_namespace(project_name, labels=labels, annotations=annotations)
    except LandingZoneError as exc:
        logger.exception("Namespace creation failed")
        return "FAILURE", "", f"Failed to create project '{project_name}': {exc}"

    # Persist the identity immediately so teardown can clean up even if a later
    # step fails.
    if resource is not None:
        resource.name = project_name
        resource.set_value_for_custom_field(CF_PREFIX + "name", project_name)
        resource.set_value_for_custom_field(CF_PREFIX + "uid", namespace["metadata"].get("uid", ""))
        resource.set_value_for_custom_field(CF_PREFIX + "display_name", display_name)
        resource.set_value_for_custom_field(CF_PREFIX + "cluster", rh.name)
        resource.set_value_for_custom_field(CF_PREFIX + "rh_id", rh.id)
        resource.set_value_for_custom_field(CF_PREFIX + "network_isolation", network_isolation)
        store_tier_on_resource(resource, tier_key, tier)
        resource.save()

    # ---- 2. ResourceQuota + 3. LimitRange -------------------------------
    set_progress(f"Applying '{tier['label']}' quota: {tier['description']}...")
    try:
        client.apply_resource_quota(project_name, quota_hard_for_tier(tier))
        client.apply_limit_range(project_name)
    except LandingZoneError as exc:
        logger.exception("Quota/LimitRange creation failed")
        return "FAILURE", "", f"Project created but quota could not be applied: {exc}"

    # ---- 4. NetworkPolicies ---------------------------------------------
    if network_isolation:
        set_progress("Applying multitenant isolation NetworkPolicies...")
        try:
            applied = client.apply_network_policies(project_name)
            set_progress(f"Applied NetworkPolicies: {', '.join(applied)}.")
        except LandingZoneError as exc:
            logger.exception("NetworkPolicy creation failed")
            return "FAILURE", "", f"Project created but NetworkPolicies could not be applied: {exc}"

    # ---- 5. Team RoleBinding --------------------------------------------
    bindings = []
    if team_group_name:
        set_progress(f"Granting OpenShift group '{team_group_name}' the admin role...")
        try:
            client.apply_role_binding(project_name, "admin", "Group", team_group_name)
            bindings.append(("admin", "Group", team_group_name))
        except (LandingZoneError, ValueError) as exc:
            logger.exception("RoleBinding creation failed")
            return "FAILURE", "", f"Project created but team access could not be granted: {exc}"

    # ---- 6. CloudBolt Environment pinned to the namespace ---------------
    set_progress("Creating a CloudBolt environment pinned to the new project...")
    try:
        env, created = ensure_environment_for_namespace(
            rh,
            project_name,
            description=f"OpenShift project '{project_name}' ({tier['label']} tier) on {rh.name}.",
            source_env=source_env,
        )
    except Exception as exc:
        logger.exception("Environment creation failed")
        return (
            "FAILURE",
            "",
            f"Project '{project_name}' was created on the cluster, but the CloudBolt "
            f"environment could not be created: {exc}",
        )
    set_progress(
        f"{'Created' if created else 'Reusing'} environment '{env.name}' (namespace '{project_name}')."
    )

    # ---- 7. Entitle groups ----------------------------------------------
    for group in (ordering_group, additional_group):
        if group is None:
            continue
        if entitle_group(env, group):
            set_progress(f"Entitled CloudBolt group '{group.name}' to environment '{env.name}'.")

    # ---- 8. Mirror quota into the Environment ---------------------------
    if set_environment_quota(env, tier):
        set_progress(
            f"Environment quota set to {tier['cpu']} cores, {tier['memory_gb']} GB memory, "
            f"{tier['storage_gb']} GB storage, {tier['vms']} VMs."
        )

    # Point the namespace back at its CloudBolt environment.
    try:
        client.patch_namespace_metadata(
            project_name, annotations={ANNOTATION_ENVIRONMENT: str(env.global_id)}
        )
    except LandingZoneError as exc:
        logger.warning("Could not annotate namespace with environment id: %s", exc)

    # ---- Persist the rest -----------------------------------------------
    console_url = client.project_console_url(project_name)
    if resource is not None:
        resource.set_value_for_custom_field(CF_PREFIX + "environment_id", env.id)
        resource.set_value_for_custom_field(CF_PREFIX + "environment_name", env.name)
        resource.set_value_for_custom_field(
            CF_PREFIX + "entitled_groups", "\n".join(entitled_group_names(env))
        )
        resource.set_value_for_custom_field(CF_PREFIX + "team_bindings", render_bindings(bindings))
        if console_url:
            resource.set_value_for_custom_field(CF_PREFIX + "console_url", console_url)
        resource.save()
        set_progress("Stored landing-zone metadata on the resource.")

    groups_text = ", ".join(entitled_group_names(env)) or "none"
    expires_text = f"; expires {expires.isoformat()}" if expires else ""
    return (
        "SUCCESS",
        f"OpenShift project '{project_name}' is ready on '{rh.name}' ({tier['label']} tier). "
        f"CloudBolt groups entitled to deploy into it: {groups_text}{expires_text}.",
        "",
    )
