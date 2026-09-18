"""
CloudBolt shared build plugin: Generate Tags from Resource Handler Tag Map.

Runs as an early deployment item of a blueprint (before the plugin that creates
the cloud object) and turns the parameters already set for the Resource into a
cloud tag dictionary, using the *tag map* the customer has configured on the
Resource Handler -- CloudBolt's Taggable Attributes
(Admin > Resource Handlers > <handler> > Tags). Each Taggable Attribute maps a
CloudBolt parameter (its "attribute") to a cloud tag name (its "label").

This is the Resource-level counterpart of what CloudBolt does natively for
servers via ResourceHandler.get_tags_dict_for_server(); the platform has no
equivalent for Resources, so this plugin fills the gap.

Output
------
A JSON object mapping tag name -> tag value is stored on the Resource in the
TXT custom field named by GENERATED_TAGS_CF ("cb_generated_tags"). Downstream
build plugins load it with json.loads(resource.get_value_for_custom_field(...))
and pass it to the cloud API. Tags are a plain dict because that is the shape
Azure ARM (ResourceGroup.tags), AWS (after list conversion) and GCP all accept.

Where parameter values come from (highest precedence first)
-----------------------------------------------------------
1. Custom field values already on the Resource. The deploy job copies every
   blueprint-level parameter on the BlueprintOrderItem onto the Resource
   before the first build item runs, so this is the primary source.
2. Arguments captured on this order for *every* build item of the blueprint
   (BlueprintItemArguments.custom_field_values). CloudBolt stores a build
   item's action inputs under a HookInput named "<name>_a<hook.id>"; the
   suffix is stripped here so a value typed on another build item's section
   of the order form is visible under its plain name.
3. Single-valued parameter defaults on the deployment Environment.
4. Single-valued parameter defaults on the ordering Group and its ancestors
   (nearest group wins).

A Taggable Attribute's "attribute" is either a custom field name or one of
CloudBolt's six curated server attributes (environment, group, hostname,
os_build, os_family, owner). hostname/os_build/os_family have no meaning for a
Resource and are skipped; group and owner are rendered exactly as the platform
renders them on servers (str() of the object) so a tag such as "Owner" reads
the same on a VM and on a resource group; environment is the deployment
Environment resolved from the order.

No action inputs
----------------
This plugin declares no action inputs, so it needs nothing on the order form
and can be reused unchanged by any blueprint. (Same-named action inputs on two
build items render as two separate fields, so sharing env_id that way is not
an option.) The Environment (and through it the Resource Handler) is resolved
from the order: a BlueprintItemArguments environment, or a parameter named
env_id / environment_id / environment found in any of the sources above.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "WARNING" | "FAILURE"
  WARNING (with an empty tag dict stored) when no environment, handler or tag
  map can be resolved, so the rest of the blueprint still builds.
"""

import datetime
import decimal
import json
import re

from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Custom field on the Resource that carries the generated tag dict (JSON).
GENERATED_TAGS_CF = "cb_generated_tags"

# Parameter names that may carry the deployment environment.
ENV_PARAM_NAMES = ("env_id", "environment_id", "environment")

# The curated (non-custom-field) attributes CloudBolt offers when creating a
# Taggable Attribute are: environment, group, hostname, os_build, os_family,
# owner. These three are Server fields with no Resource equivalent.
SERVER_ONLY_ATTRIBUTES = {"hostname", "os_build", "os_family"}

# Build-item action inputs are stored under a HookInput named "<name>_a<hook.id>".
_HOOK_INPUT_SUFFIX = re.compile(r"_a\d+$")

# Max chain length when walking job.parent_job / group.parent.
_MAX_WALK = 10


def _ensure_custom_fields():
    CustomField.objects.get_or_create(
        name=GENERATED_TAGS_CF,
        defaults=dict(
            label="Generated Tags",
            description=(
                "Cloud tags generated from the resource handler's tag map "
                "(Taggable Attributes), as a JSON object of tag name to value."
            ),
            type="TXT",
            show_on_servers=False,
        ),
    )


# --------------------------------------------------------------------------
# Value normalisation
# --------------------------------------------------------------------------
def _to_tag_value(value):
    """Render a CloudBolt parameter value as a cloud tag string, or None to skip."""
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        parts = [p for p in (_to_tag_value(v) for v in value) if p]
        return ",".join(parts) if parts else None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, decimal.Decimal)):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    # Model instances (LDAP utility, network, etc.) and everything else.
    text = str(value).strip()
    return text or None


def _cfv_to_pair(cfv):
    """Return (cf_name, value) for a CustomFieldValue, tolerant of stub shapes."""
    field = getattr(cfv, "field", None)
    name = getattr(field, "name", None)
    if not name:
        return None, None
    value = getattr(cfv, "value", None)
    return name, value


def _single_valued_options(holder):
    """Parameter defaults on a Group/Environment: only CFs with exactly one option.

    Group and Environment 'custom_field_options' double as pick-lists; a CF with
    several options is a choice for the user, not a value, so it is skipped.
    """
    result = {}
    manager = getattr(holder, "custom_field_options", None)
    if manager is None:
        return result
    grouped = {}
    for cfv in manager.all():
        name, value = _cfv_to_pair(cfv)
        if name:
            grouped.setdefault(name, []).append(value)
    for name, values in grouped.items():
        if len(values) == 1:
            result[name] = values[0]
    return result


# --------------------------------------------------------------------------
# Order context
# --------------------------------------------------------------------------
def _find_blueprint_order_item(job):
    """The BlueprintOrderItem that placed this order, or None.

    Job.order_item returns job_parameters.cast(): on the plugin job itself that
    is its HookParameters, on the parent deploy job it is the
    BlueprintOrderItem. Walk up the chain and type-check each hop, then fall
    back to the BOI id CloudBolt serialises into this job's hook context.
    """
    from orders.models import BlueprintOrderItem

    current = job
    for _ in range(_MAX_WALK):
        if current is None:
            break
        candidate = getattr(current, "order_item", None)
        if isinstance(candidate, BlueprintOrderItem):
            return candidate
        current = getattr(current, "parent_job", None)

    try:
        context = job.job_parameters.cast().arguments.get("context") or {}
        boi_id = context.get("blueprint_order_item")
        if boi_id:
            return BlueprintOrderItem.objects.filter(id=boi_id).first()
    except Exception:
        logger.debug("Could not read blueprint_order_item from the hook context", exc_info=True)
    return None


def _order_arguments(job):
    """Collect (values, environments) captured on the order across all build items.

    values: dict of plain CF name -> value; blueprint-level parameters first,
            then each build item's action inputs (suffix stripped), first
            occurrence wins.
    environments: Environment instances set directly on server-tier arguments.
    """
    values = {}
    environments = []
    boi = _find_blueprint_order_item(job)
    if boi is None:
        logger.info("No BlueprintOrderItem found for job %s; using Resource values only", job.id)
        return values, environments

    # Blueprint-level parameters live on the BlueprintOrderItem itself.
    try:
        for name, value in boi.get_cf_values_as_dict().items():
            values.setdefault(name, value)
    except Exception:
        logger.debug("Could not read CF values from BlueprintOrderItem", exc_info=True)

    try:
        item_args = list(boi.blueprintitemarguments_set.all())
    except Exception:
        logger.debug("Could not query BlueprintItemArguments", exc_info=True)
        item_args = []

    for bia in item_args:
        env = getattr(bia, "environment", None)
        if env is not None:
            environments.append(env)
        try:
            for name, value in bia.get_cf_values_as_dict().items():
                values.setdefault(_HOOK_INPUT_SUFFIX.sub("", name), value)
        except Exception:
            logger.debug("Could not read CF values from BlueprintItemArguments %s", bia.pk, exc_info=True)
    return values, environments


def _environment_from_value(value):
    """Turn an env_id-style parameter value into an Environment, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, Environment):
        return value
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    try:
        return Environment.objects.get(id=int(str(value).strip()))
    except (TypeError, ValueError, Environment.DoesNotExist):
        pass
    try:
        return Environment.objects.get(name=str(value).strip())
    except (Environment.DoesNotExist, Environment.MultipleObjectsReturned):
        return None


def _resolve_environment(resource_values, order_values, order_environments):
    if order_environments:
        return order_environments[0]
    for source in (resource_values, order_values):
        for name in ENV_PARAM_NAMES:
            env = _environment_from_value(source.get(name))
            if env is not None:
                return env
    return None


# --------------------------------------------------------------------------
# Tag map evaluation
# --------------------------------------------------------------------------
def _taggable_attributes(rh):
    """Taggable Attributes for this handler plus handler-agnostic ("global") ones.

    common.methods.get_taggable_attributes(include_globals=True) returns the
    handler's rows plus rows with resource_handler=None whose attribute the
    handler does not already map -- the same set the platform uses for servers.
    """
    try:
        from common.methods import get_taggable_attributes

        return list(get_taggable_attributes(resource_handler=rh, include_globals=True))
    except Exception:
        logger.debug("get_taggable_attributes unavailable; querying the model directly", exc_info=True)
    from tags.models import TaggableAttribute

    return list(TaggableAttribute.objects.filter(resource_handler=rh))


def _attribute_name(taggable_attribute):
    """The CloudBolt parameter name a Taggable Attribute points at.

    TaggableAttribute.attribute is a CharField holding a custom field name or
    one of the curated server attribute names.
    """
    attribute = getattr(taggable_attribute, "attribute", None)
    return getattr(attribute, "name", None) or (str(attribute).strip() if attribute else "")


def _resource_level_attribute(name, resource, env):
    """Values for the curated (non-custom-field) tag-map attributes.

    TaggableAttribute.get_tag() renders group/owner on a server as
    force_str(server.group) / force_str(server.owner); use str() here too so the
    same tag reads identically on servers and resources.
    """
    if name == "group":
        group = getattr(resource, "group", None)
        return str(group) if group is not None else None
    if name == "owner":
        owner = getattr(resource, "owner", None)
        return str(owner) if owner is not None else None
    if name == "environment":
        return str(env) if env is not None else None
    return None


def build_tag_dict(resource, env, rh, values):
    """Evaluate the handler's tag map against the merged parameter values.

    Returns (tags, skipped) where tags is {tag_name: value} and skipped is a
    list of human-readable reasons for attributes that produced no tag.
    """
    tags = {}
    skipped = []
    for ta in _taggable_attributes(rh):
        attr = _attribute_name(ta)
        label = (getattr(ta, "label", None) or attr or "").strip()
        if not attr or not label:
            skipped.append("taggable attribute %s has no attribute/label" % getattr(ta, "pk", "?"))
            continue
        if attr in SERVER_ONLY_ATTRIBUTES:
            skipped.append("%s -> %s: server-only attribute" % (attr, label))
            continue

        raw = _resource_level_attribute(attr, resource, env)
        if raw is None:
            raw = values.get(attr)
        value = _to_tag_value(raw)
        if value is None:
            skipped.append("%s -> %s: no value set" % (attr, label))
            continue
        if label in tags and tags[label] != value:
            logger.warning("Tag '%s' mapped twice; keeping '%s', ignoring '%s'", label, tags[label], value)
            continue
        tags[label] = value
    return tags, skipped


def load_generated_tags(resource):
    """Helper for downstream plugins: the generated tag dict, or {} when absent."""
    if resource is None:
        return {}
    raw = resource.get_value_for_custom_field(GENERATED_TAGS_CF)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def run(job, **kwargs):
    set_progress("Generating tags from the resource handler's tag map...")
    _ensure_custom_fields()

    resource = kwargs.get("resource") or job.resource_set.first()
    if resource is None:
        return "FAILURE", "", "No Resource is associated with this job; cannot store generated tags."

    def _store(tags):
        resource.set_value_for_custom_field(GENERATED_TAGS_CF, json.dumps(tags, sort_keys=True))
        resource.save()

    # ---- Gather parameter values, highest precedence first ---------------
    resource_values = {}
    try:
        resource_values = dict(resource.get_cf_values_as_dict())
    except Exception:
        logger.debug("Could not read CF values from the resource", exc_info=True)

    order_values, order_environments = _order_arguments(job)

    env = _resolve_environment(resource_values, order_values, order_environments)
    if env is None:
        _store({})
        return (
            "WARNING",
            "No tags generated.",
            "Could not determine the deployment Environment from the order "
            "(looked for a build-item environment or a parameter named "
            + ", ".join(ENV_PARAM_NAMES) + "). Stored an empty tag set.",
        )

    rh_base = getattr(env, "resource_handler", None)
    if rh_base is None:
        _store({})
        return "WARNING", "No tags generated.", (
            "Environment '%s' has no resource handler, so it has no tag map." % env.name
        )
    rh = rh_base.cast()
    if not getattr(rh, "can_manage_tags", True):
        # Mirrors ResourceHandler.get_tags_dict_for_server(), which returns {}
        # for handlers that cannot manage tags.
        _store({})
        return "WARNING", "No tags generated.", (
            "Resource handler '%s' does not support tag management." % rh.name
        )

    values = {}
    group = getattr(resource, "group", None)
    group_chain = []
    for _ in range(_MAX_WALK):
        if group is None:
            break
        group_chain.append(group)
        group = getattr(group, "parent", None)
    for holder in reversed(group_chain):            # root group first, nearest overrides
        values.update(_single_valued_options(holder))
    values.update(_single_valued_options(env))     # environment defaults override group
    values.update({k: v for k, v in order_values.items() if v not in (None, "")})
    values.update({k: v for k, v in resource_values.items() if v not in (None, "")})

    # ---- Evaluate the tag map --------------------------------------------
    tags, skipped = build_tag_dict(resource, env, rh, values)
    for reason in skipped:
        logger.info("Tag map: skipped %s", reason)

    _store(tags)

    if not tags:
        return (
            "WARNING",
            "No tags generated.",
            "Resource handler '%s' has no Taggable Attributes that match a parameter "
            "set on this order. Skipped: %s" % (rh.name, "; ".join(skipped) or "none"),
        )

    summary = ", ".join("%s=%s" % (k, v) for k, v in sorted(tags.items()))
    set_progress("Generated %d tag(s): %s" % (len(tags), summary))
    return (
        "SUCCESS",
        "Generated %d tag(s) from the tag map on handler '%s': %s" % (len(tags), rh.name, summary),
        "",
    )
