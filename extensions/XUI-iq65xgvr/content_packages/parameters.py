"""
Standalone parameters in content packages.

CloudBolt's Source Control Repos has no folder for a parameter (SourceCodeRepo
has no path_to_* for one), so a parameter reaches a repository only inside a
blueprint's parameters[] or an action's action_inputs[]. A package may list
one as "parameters/CF-<id>"; the extension then writes and reads
parameters/CF-<id>/CF-<id>_metadata.json itself, with the serializer
CloudBolt's blueprint export and import use for each parameters[] entry
(infrastructure.api.v3.serializers.CustomFieldSerializer). The file therefore
holds what a blueprint export writes for that parameter:

- Export: resource_dict(exporting=True, include_dependencies=True), plus the
  parameter's global defaults and constraints as the Parameters API returns
  them. Each orchestration action that generates the parameter's options is
  pushed with CloudBolt's own per-object push and referenced as
  "orchestration_actions/HPA-<id>", as a blueprint export references it.
- Sync: those actions sync first, each in its own sourcecodereposync job; a
  job then runs get_or_create_from_dict, the call blueprint import makes,
  which matches an existing parameter by name, and links the actions to it
  the way blueprint import does.

Assignments to environments, groups and blueprints are bindings to one
appliance and stay out, as CloudBolt leaves them out of every export. So do
option and default values of secret types and of types whose values are
objects on the appliance.
"""
import copy
import json
import posixpath
from datetime import date

from cbhooks.models import HookPointAction
from infrastructure.models import CustomField

from xui.content_packages import store

GEN_OPTIONS_HOOK_POINT = "Generated Parameter Options"

# Option and default values of these types are secrets.
SECRET_TYPES = ("PWD", "ETXT")
# Values of these types are objects on the appliance: a network, an LDAP
# domain, an NSX object, a storage, a file, an Ansible Automation Platform.
OBJECT_TYPES = ("NET", "LDAP", "NSXS", "NSXE", "STOR", "FILE", "AAP")

# What CustomFieldSerializer.get_or_create_from_dict takes: the name it
# matches on, the global ID it keeps when it creates, the OS families and
# field dependencies it recreates, and the fields the Parameters API creates
# a parameter with. Everything else in the file is applied separately.
IMPORT_FIELDS = (
    "name", "id", "relevant_osfamilies",
    "field_dependency_controlling_set", "field_dependency_dependent_set",
    "label", "placeholder", "description", "type", "allow_multiple", "required",
    "show_on_servers", "available_all_servers", "show_as_attribute", "value_pattern_string",
)


def metadata_path(global_id):
    return "{0}/{1}/{1}_metadata.json".format(store.PARAMETERS_DIR, global_id)


def dumps(data):
    """The file's text, formatted like every CloudBolt export. Values the
    serializer returns as dates or decimals are written as text."""
    return json.dumps(data, indent=4, sort_keys=True, default=str) + "\n"


def keeps_values(cf):
    return cf.type not in SECRET_TYPES + OBJECT_TYPES


def gen_options_actions(cf):
    """The orchestration actions that generate cf's options, in the order the
    serializer lists them."""
    return [
        hpa
        for hook in cf.orchestration_hooks.all()
        for hpa in hook.hookpointaction_set.filter(hook_point__label=GEN_OPTIONS_HOOK_POINT)
    ]


def _action_ref(hpa, repo):
    folder = (getattr(repo, "path_to_orchestration_actions", "") or "orchestration_actions/").strip("/")
    return posixpath.join(folder, hpa.global_id)


def export_metadata(cf, repo, global_id):
    """The metadata file for cf, as a dict. repo supplies the folder of the
    generated-options actions; global_id is the package's ID for the
    parameter, which is not cf's own when a sync matched cf by name."""
    from infrastructure.api.v3.serializers import CustomFieldSerializer
    from infrastructure.services import CustomFieldService

    data = dict(CustomFieldSerializer().resource_dict(cf, exporting=True, include_dependencies=True))
    hooks = []
    for entry in data.get("gen_options_hooks") or []:
        entry = dict(entry)
        hpa = (entry.get("dependencies") or {}).get("orchestration_hook")
        if hpa is not None:
            entry["dependencies"] = {"orchestration_hook": _action_ref(hpa, repo)}
        hooks.append(entry)
    if hooks:
        data["gen_options_hooks"] = hooks
    service = CustomFieldService(cf)
    data["global_constraints"] = service.get_global_constraints()
    if keeps_values(cf):
        data["global_defaults"] = service.get_global_defaults()
    else:
        data.pop("global_options", None)
    data["id"] = global_id
    data["last_updated"] = date.today().isoformat()
    return data


def readme(cf, data):
    """The README written the first time a parameter's folder is exported."""
    lines = ["# {}".format(cf.label or cf.name), ""]
    description = " ".join((cf.description or "").split())
    if description:
        lines += [description, ""]
    lines += [
        "Parameter `{}` ({}). Source Control Repos does not sync parameters: the Content Package "
        "Manager UI extension writes this folder when it exports a package that lists the parameter, "
        "and its Sync creates the parameter, or updates the one with this name.".format(
            cf.name, cf.get_type_display()
        ),
        "",
    ]
    hooks = data.get("gen_options_hooks") or []
    if hooks:
        lines += ["Options generated by:", ""]
        for entry in hooks:
            ref = (entry.get("dependencies") or {}).get("orchestration_hook")
            name = (entry.get("name") or ref or "").replace("]", "\\]")
            lines.append("- [{}](../../{}/)".format(name, ref) if ref else "- {}".format(name))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def read_metadata(clone, branch, global_id):
    """The parameter's metadata on branch, read from a fetched global clone.
    Raises PackageError when it is missing or unreadable."""
    path = metadata_path(global_id)
    try:
        data = json.loads(clone.git.show("origin/{}:{}".format(branch, path)))
    except Exception:
        raise store.PackageError(
            "Branch {} has no readable {}. Export the package from an appliance that has the parameter "
            "to write it.".format(branch, path)
        )
    if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not data["name"]:
        raise store.PackageError("{} on branch {} names no parameter.".format(path, branch))
    return data


def action_ids(data):
    """Global IDs of the generated-options actions a parameter's metadata
    references, in order."""
    ids = []
    for entry in data.get("gen_options_hooks") or []:
        ref = ((entry or {}).get("dependencies") or {}).get("orchestration_hook")
        if isinstance(ref, str) and ref.strip("/"):
            global_id = posixpath.basename(ref.strip("/"))
            if global_id.startswith("HPA-") and global_id not in ids:
                ids.append(global_id)
    return ids


def _link_actions(cf, data):
    """Replace cf's generated-options actions with the ones data lists, as
    blueprint import does: unlink the plugins of its current ones, then link
    each listed action's plugin. An action is found by global ID, else by
    name. Returns the names of the actions that were not found."""
    current = cf.orchestration_hooks.filter(hookpointaction__hook_point__label=GEN_OPTIONS_HOOK_POINT).distinct()
    for hook in list(current):
        cf.orchestration_hooks.remove(hook)
    missing = []
    for entry in data.get("gen_options_hooks") or []:
        entry = entry or {}
        ref = (entry.get("dependencies") or {}).get("orchestration_hook")
        global_id = posixpath.basename(ref.strip("/")) if isinstance(ref, str) else ""
        hpa = HookPointAction.objects.filter(global_id=global_id).first() if global_id else None
        if hpa is None and entry.get("name"):
            hpa = HookPointAction.objects.filter(
                name=entry["name"], hook_point__label=GEN_OPTIONS_HOOK_POINT
            ).first()
        if hpa is None or hpa.hook is None:
            missing.append(entry.get("name") or global_id or "?")
            continue
        hpa.hook.custom_fields.add(cf)
    return missing


def _waiting_dependencies(data):
    """Parameters data's field dependencies name that do not exist here, so
    those dependencies were not created."""
    names = set()
    for key in ("field_dependency_controlling_set", "field_dependency_dependent_set"):
        for dependency in data.get(key) or []:
            for side in ("controlling-field", "dependent-field"):
                name = ((dependency or {}).get(side) or {}).get("name")
                if name and name != data.get("name"):
                    names.add(name)
    found = set(CustomField.objects.filter(name__in=names).values_list("name", flat=True))
    return sorted(names - found)


def import_metadata(data, refresh_if_exists):
    """Create the parameter data describes, or update the parameter with its
    name when refresh_if_exists. Returns (cf, outcome, notes): outcome is
    "created", "updated" or "unchanged", and notes are warnings worded for the
    job log."""
    from infrastructure.api.v3.serializers import CustomFieldSerializer

    serializer = CustomFieldSerializer()
    name = data["name"]
    existing = CustomField.objects.filter(name=name).first()
    if existing is not None and not refresh_if_exists:
        return existing, "unchanged", [
            "Parameter {} already exists here; it was left unchanged because refreshing existing "
            "content was off.".format(name)
        ]
    if existing is not None and data.get("type") and data["type"] != existing.type:
        raise store.PackageError(
            "Parameter {} is {} here but {} in the repository, and CloudBolt cannot change a parameter's "
            "type, so it was not updated.".format(name, existing.type, data["type"])
        )

    fields = {key: copy.deepcopy(data[key]) for key in IMPORT_FIELDS if key in data}
    cf = serializer.get_or_create_from_dict(fields, replace_existing=existing is not None)
    notes = []
    if keeps_values(cf):
        if isinstance(data.get("global_options"), list):
            serializer.update_global_options(cf, data["global_options"])
        if isinstance(data.get("global_defaults"), list):
            try:
                serializer.update_global_defaults(cf, data["global_defaults"])
            except Exception as exc:
                notes.append("Global defaults were not set: {}.".format(str(exc) or type(exc).__name__))
    if isinstance(data.get("global_constraints"), dict):
        serializer.update_global_constraints(cf, data["global_constraints"])

    missing = _link_actions(cf, data)
    if missing:
        notes.append("Generated-options action{} {} not found, so the options are not generated.".format(
            "" if len(missing) == 1 else "s", ", ".join(missing)
        ))
    waiting = _waiting_dependencies(data)
    if waiting:
        notes.append(
            "Dependencies on {} were not created because no parameter with that name exists here yet; "
            "they are created when it is synced.".format(", ".join(waiting))
        )
    return cf, "created" if existing is None else "updated", notes
