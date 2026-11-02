"""
Which servers and images a credentials update reaches, and what it changes on
each.

Filters combine with AND; the values picked in one filter combine with OR
("any of"), and an empty filter does not narrow. Servers must be Active and
outside the Unassigned groups and environments, which hold servers that have
not been onboarded. Group and blueprint filters narrow servers only, because
images belong to neither.

The preview and the update job share this module, so the job changes exactly
the objects the preview listed: the preview returns a token (a hash of the
target IDs and the requested changes), and the update refuses to start when
the targets no longer hash to it.
"""
import hashlib
import json
from collections import Counter

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.urls import reverse

from accounts.models import Group
from externalcontent.models import OSBuild, OSBuildAttribute, OSFamily
from infrastructure.models import Environment, Server
from resourcehandlers.models import ResourceHandler
from resourcehandlers.vmware.models import VsphereResourceHandler
from servicecatalog.models import ServiceBlueprint
from utilities.models import SSHKey

FILTER_KEYS = (
    "technologies", "handlers", "os_builds", "os_families",
    "environments", "groups", "blueprints", "usernames",
)
# Filters that narrow servers only.
SERVER_ONLY_FILTERS = ("groups", "blueprints")

# The parameter (CustomField) names CloudBolt keeps credentials in. A server
# falls back to the legacy names when the current ones are not set
# (ResourceHandler.get_server_credentials); the native credentials dialog
# removes the legacy ones when it saves (EditServerCredentialsForm.save).
USERNAME_CF = "username"
PASSWORD_CF = "password"
KEY_CF = "key_name"
NEW_VM_PASSWORD_CF = "new_password"
LEGACY_USERNAME_CFS = ("initial_username",)
LEGACY_PASSWORD_CFS = (
    "initial_password", "initial_linux_password", "initial_windows_password", "template_root_password",
)

# Warning code -> text shown in the preview.
WARNINGS = {
    "no_username": "No username stored and none entered: CloudBolt will log in as its default "
                   "(root, ubuntu or Administrator).",
    "key_unavailable": "The SSH key is not available here (it belongs to another resource handler "
                       "or environment), so it is not applied.",
    "key_windows": "Windows: the SSH key is not applied.",
    "not_vmware": "Not VMware: the new VM password is not applied (only the VMware handler sets it "
                  "during provisioning).",
}

# Change code -> column text in the CSV.
CHANGE_LABELS = {
    "username": "Username", "password": "Password", "ssh_key": "SSH key", "new_vm_password": "New VM password",
}

# Facet key -> label, per kind, in display order.
SERVER_FACETS = (
    ("handler", "Resource handler"), ("os_build", "OS build"), ("environment", "Environment"),
    ("group", "Group"), ("blueprint", "Blueprint"), ("username", "Current username"),
    ("power", "Power"),
)
IMAGE_FACETS = (
    ("handler", "Resource handler"), ("os_build", "OS build"), ("username", "Current username"),
)

NONE_LABEL = "(none)"
NO_USERNAME_LABEL = "(none stored)"
# Filter value for "no username stored"; Selectize drops options whose value is empty.
NO_USERNAME = "__none__"


class TargetError(Exception):
    """A request the page should show as an error, not log as a crash."""


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------
def _ids(values):
    out = []
    for value in values or []:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            raise TargetError("Filter values must be IDs.")
    return out


def parse_filters(data):
    """The filters dict of a request body, with every key present."""
    raw = data.get("filters") or {}
    if not isinstance(raw, dict):
        raise TargetError("filters must be an object.")
    filters = {key: _ids(raw.get(key)) for key in FILTER_KEYS if key != "usernames"}
    filters["usernames"] = ["" if str(v) == NO_USERNAME else str(v) for v in raw.get("usernames") or []]
    return filters


def parse_scope(data):
    """(include servers, include images) from a request body."""
    scope = data.get("apply_to") or {}
    servers, images = bool(scope.get("servers")), bool(scope.get("images"))
    if not servers and not images:
        raise TargetError("Choose existing servers, images, or both.")
    return servers, images


def parse_plan(data):
    """What the update changes, without the secrets: the preview needs to know
    which fields are filled in, never their values."""
    changes = data.get("changes") or {}
    key = None
    if changes.get("ssh_key"):
        key = SSHKey.objects.filter(id=_ids([changes["ssh_key"]])[0]).select_related(
            "key_reference", "resource_handler"
        ).first()
        if key is None:
            raise TargetError("That SSH key no longer exists.")
    return {
        "username": str(changes.get("username") or "").strip(),
        "password": bool(changes.get("password")),
        "key": key,
        "new_vm_password": bool(changes.get("new_vm_password")),
    }


# ---------------------------------------------------------------------------
# Lookups shared by both kinds
# ---------------------------------------------------------------------------
def _family_roots():
    """({OS family id: name of its root family}, {OS family id: values row})."""
    families = {f["id"]: f for f in OSFamily.objects.values("id", "name", "parent_id")}

    def root(fid):
        seen = set()
        while families[fid]["parent_id"] in families and fid not in seen:
            seen.add(fid)
            fid = families[fid]["parent_id"]
        return families[fid]["name"]

    return {fid: root(fid) for fid in families}, families


def _expand_families(selected, families):
    """The selected families plus every family below them."""
    out, todo = set(selected), list(selected)
    while todo:
        parent = todo.pop()
        for fid, fam in families.items():
            if fam["parent_id"] == parent and fid not in out:
                out.add(fid)
                todo.append(fid)
    return out


def _cfv_strings(model, owner_qs, cf_names):
    """{owner id: {cf name: str value}} for the given parameters, read through
    the custom_field_values join table in one query."""
    m2m = model._meta.get_field("custom_field_values")
    through = m2m.remote_field.through
    src, dst = m2m.m2m_field_name(), m2m.m2m_reverse_field_name()
    rows = through.objects.filter(
        **{src + "__in": owner_qs.values("id"), dst + "__field__name__in": cf_names}
    ).values_list(src + "_id", dst + "__field__name", dst + "__str_value")
    out = {}
    for owner_id, name, value in rows:
        out.setdefault(owner_id, {})[name] = value
    return out


def _key_scope(key):
    """For the chosen SSH key: (None, tenant id) for a global key, which fits
    every object in that tenant, else (handler id, environment ids whose
    options include the key), mirroring Server.get_sshkey_options and
    OSBuildAttribute.get_sshkey_options."""
    if key.resource_handler_id is None:
        return None, key.tenant_id
    envs = set(Environment.objects.filter(
        resource_handler_id=key.resource_handler_id, custom_field_options=key.key_reference_id
    ).values_list("id", flat=True))
    return key.resource_handler_id, envs


def _vmware_handler_ids():
    """Ids of the handlers whose provisioning sets the new VM password: vCenter
    and its subclasses (VMware Cloud on AWS). Read from each handler's content
    type, because loading every handler's subclass row (type_slug casts) took
    two seconds on a lab appliance."""
    out = set()
    for rh_id, type_id in ResourceHandler.objects.values_list("id", "real_type_id"):
        model = ContentType.objects.get_for_id(type_id).model_class() if type_id else None
        if model and issubclass(model, VsphereResourceHandler):
            out.add(rh_id)
    return out


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------
def server_queryset(filters, families=None):
    qs = Server.objects.filter(
        status="ACTIVE", resource_handler__isnull=False, group__isnull=False, environment__isnull=False
    ).exclude(group__in=Group.unassigned.all()).exclude(environment__in=Environment.unassigned.all())
    if filters["technologies"]:
        qs = qs.filter(resource_handler__resource_technology__in=filters["technologies"])
    if filters["handlers"]:
        qs = qs.filter(resource_handler__in=filters["handlers"])
    if filters["os_builds"]:
        qs = qs.filter(os_build__in=filters["os_builds"])
    if filters["os_families"]:
        if families is None:
            families = _family_roots()[1]
        qs = qs.filter(os_family__in=_expand_families(filters["os_families"], families))
    if filters["environments"]:
        qs = qs.filter(environment__in=filters["environments"])
    if filters["groups"]:
        qs = qs.filter(group__in=filters["groups"])
    if filters["blueprints"]:
        # A server built by a blueprint with a resource type hangs off the
        # resource; one built by a blueprint without one has only its service item.
        qs = qs.filter(
            Q(resource__blueprint__in=filters["blueprints"]) | Q(service_item__blueprint__in=filters["blueprints"])
        )
    return qs


POWER_LABELS = {"POWERON": "On", "POWEROFF": "Off"}

SERVER_VALUES = (
    "id", "hostname", "power_status", "resource_handler_id", "resource_handler__name",
    "environment_id", "environment__name", "environment__tenant_id", "group_id", "group__name",
    "os_build_id", "os_build__name", "os_family_id",
    "resource__blueprint_id", "resource__blueprint__name",
    "service_item__blueprint_id", "service_item__blueprint__name",
)


def _server_username(cfvs):
    """The username CloudBolt logs in with when it uses stored values only."""
    if cfvs.get(USERNAME_CF):
        return cfvs[USERNAME_CF]
    for name in LEGACY_USERNAME_CFS:
        if cfvs.get(name):
            return cfvs[name]
    return ""


def servers(filters, plan):
    """Target rows for servers, sorted by hostname."""
    roots, families = _family_roots()
    qs = server_queryset(filters, families)
    usernames = _cfv_strings(Server, qs, (USERNAME_CF,) + LEGACY_USERNAME_CFS)
    key = plan["key"]
    key_scope = _key_scope(key) if key else None
    wanted = set(filters["usernames"])
    rows = []
    for s in qs.values(*SERVER_VALUES).order_by("hostname", "id"):
        current = _server_username(usernames.get(s["id"], {}))
        if wanted and current not in wanted:
            continue
        windows = roots.get(s["os_family_id"]) == "Windows"
        changes, warnings = [], []
        if plan["username"] and plan["username"] != current:
            changes.append("username")
        if plan["password"]:
            changes.append("password")
        if key:
            if windows:
                warnings.append("key_windows")
            elif not _key_fits(key_scope, s["resource_handler_id"], [s["environment_id"]], s["environment__tenant_id"]):
                warnings.append("key_unavailable")
            else:
                changes.append("ssh_key")
        if not plan["username"] and not current and changes:
            warnings.append("no_username")
        blueprint_id = s["resource__blueprint_id"] or s["service_item__blueprint_id"]
        rows.append({
            "kind": "server",
            "id": s["id"],
            "name": s["hostname"] or "Server {}".format(s["id"]),
            "url": reverse("server_detail", args=[s["id"]]),
            "handler": s["resource_handler__name"],
            "os_build": s["os_build__name"] or NONE_LABEL,
            "environment": s["environment__name"],
            "group": s["group__name"],
            "blueprint": (s["resource__blueprint__name"] or s["service_item__blueprint__name"]) if blueprint_id else NONE_LABEL,
            "username": current or NO_USERNAME_LABEL,
            "power": POWER_LABELS.get(s["power_status"], (s["power_status"] or "Unknown").title()),
            "changes": changes,
            "warnings": warnings,
        })
    return rows


def _key_fits(key_scope, handler_id, env_ids, tenant_id):
    scope_handler, scope = key_scope
    if scope_handler is None:
        return scope == tenant_id
    return handler_id == scope_handler and bool(scope.intersection(env_ids))


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def image_queryset(filters, families=None):
    qs = OSBuildAttribute.objects.filter(resourcehandler__isnull=False)
    if filters["technologies"]:
        qs = qs.filter(resourcehandler__resource_technology__in=filters["technologies"])
    if filters["handlers"]:
        qs = qs.filter(resourcehandler__in=filters["handlers"])
    if filters["os_builds"]:
        qs = qs.filter(os_build__in=filters["os_builds"])
    if filters["os_families"]:
        if families is None:
            families = _family_roots()[1]
        qs = qs.filter(os_build__os_family__in=_expand_families(filters["os_families"], families))
    return qs


def _build_environments(os_build_ids):
    """{os build id: [(environment id, handler id), ...]} for the environments
    each OS build is available in."""
    m2m = OSBuild._meta.get_field("environments")
    through = m2m.remote_field.through
    src, dst = m2m.m2m_field_name(), m2m.m2m_reverse_field_name()
    out = {}
    rows = through.objects.filter(**{src + "_id__in": list(os_build_ids)}).values_list(
        src + "_id", dst + "_id", dst + "__resource_handler_id"
    )
    for build_id, env_id, handler_id in rows:
        out.setdefault(build_id, []).append((env_id, handler_id))
    return out


def images(filters, plan):
    """Target rows for images, sorted by handler and name. An image fits an
    environment filter when its OS build is offered in one of the chosen
    environments on the image's own handler."""
    roots, families = _family_roots()
    qs = image_queryset(filters, families)
    values = list(qs.values(
        "id", "template_name", "resourcehandler_id", "resourcehandler__name",
        "resourcehandler__tenant_id", "os_build_id", "os_build__name", "os_build__os_family_id",
    ))
    build_envs = _build_environments({v["os_build_id"] for v in values if v["os_build_id"]})
    usernames = _cfv_strings(OSBuildAttribute, qs, (USERNAME_CF,))
    vmware = _vmware_handler_ids()
    key = plan["key"]
    key_scope = _key_scope(key) if key else None
    wanted_envs = set(filters["environments"])
    wanted_names = set(filters["usernames"])
    rows = []
    for v in values:
        envs = [env for env, handler in build_envs.get(v["os_build_id"], []) if handler == v["resourcehandler_id"]]
        if wanted_envs and not wanted_envs.intersection(envs):
            continue
        current = usernames.get(v["id"], {}).get(USERNAME_CF) or ""
        if wanted_names and current not in wanted_names:
            continue
        windows = roots.get(v["os_build__os_family_id"]) == "Windows"
        changes, warnings = [], []
        if plan["username"] and plan["username"] != current:
            changes.append("username")
        if plan["password"]:
            changes.append("password")
        if key:
            if windows:
                warnings.append("key_windows")
            elif not _key_fits(key_scope, v["resourcehandler_id"], envs, v["resourcehandler__tenant_id"]):
                warnings.append("key_unavailable")
            else:
                changes.append("ssh_key")
        if plan["new_vm_password"]:
            if v["resourcehandler_id"] in vmware:
                changes.append("new_vm_password")
            else:
                warnings.append("not_vmware")
        rows.append({
            "kind": "image",
            "id": v["id"],
            "name": v["template_name"] or v["os_build__name"] or "Image {}".format(v["id"]),
            "url": reverse("resourcehandler_detail", args=[v["resourcehandler_id"]]),
            "handler": v["resourcehandler__name"],
            "os_build": v["os_build__name"] or NONE_LABEL,
            "username": current or NO_USERNAME_LABEL,
            "changes": changes,
            "warnings": warnings,
        })
    rows.sort(key=lambda r: (r["handler"].lower(), r["name"].lower()))
    return rows


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
def collect(data):
    """(filters, plan, server rows, image rows) for a request body."""
    filters = parse_filters(data)
    include_servers, include_images = parse_scope(data)
    plan = parse_plan(data)
    if not (plan["username"] or plan["password"] or plan["key"] or (include_images and plan["new_vm_password"])):
        raise TargetError("Enter a username, a password, or an SSH key to apply.")
    server_rows = servers(filters, plan) if include_servers else []
    image_rows = images(filters, plan) if include_images else []
    return filters, plan, server_rows, image_rows


def token(server_rows, image_rows, plan):
    """Hash of what would change; the update must see the same one."""
    payload = {
        "servers": sorted((r["id"], tuple(r["changes"])) for r in server_rows if r["changes"]),
        "images": sorted((r["id"], tuple(r["changes"])) for r in image_rows if r["changes"]),
        "username": plan["username"],
        "key": plan["key"].id if plan["key"] else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _facets(rows, facet_keys):
    """[{key, label, values: [{value, count}]}] sorted by count, largest first."""
    out = []
    for key, label in facet_keys:
        counts = Counter(r[key] for r in rows)
        out.append({
            "key": key, "label": label,
            "values": [{"value": v, "count": c} for v, c in sorted(counts.items(), key=lambda i: (-i[1], str(i[0]).lower()))],
        })
    return out


def _summary(rows, facet_keys, plan, key_text):
    changing = [r for r in rows if r["changes"]]
    username_from = Counter(r["username"] for r in changing if "username" in r["changes"])
    changes = []
    if plan["username"]:
        for current, count in username_from.most_common():
            changes.append({"text": "Username {} → {}".format(current, plan["username"]), "count": count})
        same = sum(1 for r in rows if r["username"] == plan["username"])
        if same:
            changes.append({"text": "Username already {}".format(plan["username"]), "count": same, "muted": True})
    for field, text in (("password", "Password set"), ("ssh_key", "SSH key set to {}".format(key_text)),
                        ("new_vm_password", "New VM password set")):
        count = sum(1 for r in changing if field in r["changes"])
        if count:
            changes.append({"text": text, "count": count})
    warnings = Counter(w for r in rows for w in r["warnings"])
    return {
        "total": len(rows),
        "changing": len(changing),
        "unchanged": len(rows) - len(changing),
        "changes": changes,
        "warnings": [{"code": code, "text": WARNINGS[code], "count": count} for code, count in warnings.most_common()],
        "facets": _facets(rows, facet_keys),
    }


def preview(data):
    filters, plan, server_rows, image_rows = collect(data)
    include_servers, include_images = parse_scope(data)
    key_text = key_label(plan["key"]) if plan["key"] else ""
    return {
        "token": token(server_rows, image_rows, plan),
        "servers": _summary(server_rows, SERVER_FACETS, plan, key_text) if include_servers else None,
        "images": _summary(image_rows, IMAGE_FACETS, plan, key_text) if include_images else None,
        "server_only_filters": [k for k in SERVER_ONLY_FILTERS if filters[k]] if include_images else [],
    }


def page(data):
    """One page of target rows, narrowed by an optional search string, facet
    value or warning code."""
    _filters, _plan, server_rows, image_rows = collect(data)
    rows = server_rows if data.get("kind") != "image" else image_rows
    rows = narrow(rows, data)
    try:
        number = max(int(data.get("page") or 1), 1)
        size = min(max(int(data.get("page_size") or 50), 10), 200)
    except (TypeError, ValueError):
        raise TargetError("page and page_size must be numbers.")
    start = (number - 1) * size
    return {
        "total": len(rows),
        "page": number,
        "page_size": size,
        "rows": [dict(r, warnings=[WARNINGS[w] for w in r["warnings"]]) for r in rows[start:start + size]],
    }


def narrow(rows, data):
    search = str(data.get("search") or "").strip().lower()
    facet = data.get("facet") or {}
    warning = data.get("warning") or ""
    status = data.get("status") or ""
    if facet.get("key"):
        rows = [r for r in rows if str(r.get(facet["key"])) == str(facet.get("value"))]
    if warning:
        rows = [r for r in rows if warning in r["warnings"]]
    if status == "changing":
        rows = [r for r in rows if r["changes"]]
    elif status == "unchanged":
        rows = [r for r in rows if not r["changes"]]
    if search:
        rows = [r for r in rows if search in " ".join(str(v) for k, v in r.items() if isinstance(v, str)).lower()]
    return rows


def describe(filters, include_servers, include_images, plan):
    """One line naming the scope and the changes, for the job and the History
    tab. Never includes a secret."""
    lookups = {
        "technologies": ("Technology", lambda ids: ResourceHandler.objects.filter(resource_technology__in=ids)
                         .values_list("resource_technology__name", flat=True).distinct()),
        "handlers": ("Resource handler", lambda ids: ResourceHandler.objects.filter(id__in=ids).values_list("name", flat=True)),
        "os_builds": ("OS build", lambda ids: OSBuild.objects.filter(id__in=ids).values_list("name", flat=True)),
        "os_families": ("OS family", lambda ids: OSFamily.objects.filter(id__in=ids).values_list("name", flat=True)),
        "environments": ("Environment", lambda ids: Environment.objects.filter(id__in=ids).values_list("name", flat=True)),
        "groups": ("Group", lambda ids: Group.objects.filter(id__in=ids).values_list("name", flat=True)),
        "blueprints": ("Blueprint", lambda ids: ServiceBlueprint.objects.filter(id__in=ids).values_list("name", flat=True)),
    }
    parts =[" and ".join(p for p, on in (("Servers", include_servers), ("images", include_images)) if on).capitalize()]
    for key, (label, lookup) in lookups.items():
        if filters[key]:
            parts.append("{}: {}".format(label, ", ".join(sorted(lookup(filters[key]), key=str.lower))))
    if filters["usernames"]:
        parts.append("Current username: {}".format(", ".join(n or NO_USERNAME_LABEL for n in filters["usernames"])))
    changes = []
    if plan["username"]:
        changes.append("username {}".format(plan["username"]))
    if plan["password"]:
        changes.append("password")
    if plan["key"]:
        changes.append("SSH key {}".format(key_label(plan["key"])))
    if plan["new_vm_password"] and include_images:
        changes.append("new VM password")
    return "{}. Changes: {}.".format(" · ".join(parts), ", ".join(changes))


# ---------------------------------------------------------------------------
# Filter options
# ---------------------------------------------------------------------------
def key_label(key):
    if key.resource_handler_id:
        return "{} ({})".format(key.name, key.resource_handler.name)
    return key.name


def options():
    """Everything the filter pickers offer, with the links the page uses to
    narrow one picker by another. Only values some onboarded server or some
    image carries are offered."""
    base = server_queryset({key: [] for key in FILTER_KEYS})
    handlers = list(ResourceHandler.objects.select_related("resource_technology").order_by("name"))
    image_pairs = set(OSBuildAttribute.objects.filter(resourcehandler__isnull=False, os_build__isnull=False)
                      .values_list("os_build_id", "resourcehandler_id"))
    server_pairs = set(base.filter(os_build__isnull=False).values_list("os_build_id", "resource_handler_id"))
    build_handlers = {}
    for build_id, handler_id in image_pairs | server_pairs:
        build_handlers.setdefault(build_id, set()).add(handler_id)
    builds = OSBuild.objects.filter(id__in=build_handlers).order_by("name")
    blueprint_ids = {i for pair in base.values_list("resource__blueprint_id", "service_item__blueprint_id") for i in pair if i}
    server_names = {_server_username(v) for v in _cfv_strings(Server, base, (USERNAME_CF,) + LEGACY_USERNAME_CFS).values()}
    image_names = {v.get(USERNAME_CF) for v in _cfv_strings(
        OSBuildAttribute, OSBuildAttribute.objects.filter(resourcehandler__isnull=False), (USERNAME_CF,)
    ).values()}
    names = sorted({n for n in server_names | image_names if n}, key=str.lower)
    technologies = {}
    for rh in handlers:
        if rh.resource_technology_id:
            technologies[rh.resource_technology_id] = rh.resource_technology.name
    return {
        "technologies": [{"id": i, "name": n} for i, n in sorted(technologies.items(), key=lambda t: t[1].lower())],
        "handlers": [{"id": rh.id, "name": rh.name, "technology": rh.resource_technology_id} for rh in handlers],
        "os_builds": [{"id": b.id, "name": b.name, "handlers": sorted(build_handlers[b.id])} for b in builds],
        "os_families": [{"id": f.id, "name": f.name} for f in OSFamily.objects.order_by("name")],
        "environments": [
            {"id": e.id, "name": e.name, "handler": e.resource_handler_id}
            for e in Environment.without_unassigned.filter(resource_handler__isnull=False).order_by("name")
        ],
        "groups": [{"id": g.id, "name": g.name} for g in Group.without_unassigned.order_by("name")],
        "blueprints": [{"id": b.id, "name": b.name} for b in ServiceBlueprint.objects.filter(id__in=blueprint_ids).order_by("name")],
        "usernames": [{"id": NO_USERNAME, "name": NO_USERNAME_LABEL}] + [{"id": n, "name": n} for n in names],
        "ssh_keys": [
            {"id": k.id, "name": key_label(k)}
            for k in sorted(SSHKey.objects.select_related("key_reference", "resource_handler"), key=lambda k: key_label(k).lower())
        ],
    }
