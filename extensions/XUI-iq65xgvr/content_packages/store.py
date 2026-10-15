"""
Content package store

A content package is a named list of top-level content units, written as the
repo's cross-reference strings ("<top_level_dir>/<GLOBAL_ID>").

Packages live in the database as values of one CustomField,
content_package_store (Multi-line Text, created on first write), in the
content_packages Namespace. A namespaced field stays off the Parameters list
and the blueprint parameter picker. Each package is one CustomFieldValue
attached to no object, holding the full package JSON: the manifest an export
will write to packages/<PKG-id>/ in a repository, plus a "local" block (home
repository and branch, export and sync history) that stays on this appliance.
The PKG- id inside the JSON is the key; the CustomFieldValue pk is only a
local handle, and rows are updated in place. Re-syncing or removing the
extension leaves the definitions alone; deleting the CustomField deletes them
all, and exported manifests are the backup.

Every write runs in a transaction holding a row lock on the CustomField, which
is also where PKG ids and package names are kept unique.

Member names are never stored: they are resolved from the appliance on every
read, so the page can flag members that were deleted or that live on another
repository or branch.
"""
import json
import re
from collections import OrderedDict
from contextlib import contextmanager
from datetime import date

from django.db import transaction

from cbhooks.models import (
    FlowControlAction,
    HookPointAction,
    InboundWebHook,
    MCPToolAction,
    OrchestrationHook,
    RecurringActionJob,
    ResourceAction,
    ServerAction,
    SharedModule,
)
from common.mixins import get_global_id_chars
from cscv.models import ActionCITTest
from extensions.models import UIExtension
from infrastructure.models import CustomField, Namespace
from orders.models import CustomFieldValue
from servicecatalog.models import ServiceBlueprint
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

STORE_NAMESPACE = "content_packages"
STORE_FIELD = "content_package_store"
STORE_FIELD_LABEL = "Content package store"
STORE_FIELD_DESCRIPTION = (
    "Managed by the Content Package Manager UI extension: one value per content package. "
    "Do not delete; deleting it deletes every package definition on this appliance."
)
# CustomFieldValue keeps both of these types in txt_value (a TextField).
STORE_FIELD_TYPES = ("TXT", "CODE")

ID_PREFIX = "PKG"
METADATA_VERSION = "2026.1.0"
DEFAULT_VERSION = "1.0.0"

PKG_ID_RE = re.compile(r"^PKG-[a-z0-9]{8,}$")
REF_RE = re.compile(r"^(?P<dir>[a-z_]+)/(?P<id>(?P<prefix>[A-Z]{2,4})-[a-z0-9]{8,})$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$")
NAME_MAX = 255
DESCRIPTION_MAX = 1000

# Fields a caller may set; the rest of the manifest is managed here.
EDITABLE_FIELDS = ("name", "description", "version", "contents")

# CloudBolt imports forms and form functions only as dependencies of a parent,
# so listing one as a member would do nothing on sync.
TRANSITIVE_DIRS = ("forms", "form_functions")


class PackageError(Exception):
    """Invalid input or a missing package; the message is safe to show."""


class PackageNotFound(PackageError):
    pass


class MemberType(object):
    """One kind of content a package may list, keyed by its repo dir."""

    def __init__(self, dir, prefix, title, singular, model, name_attr, picker=None):
        self.dir = dir
        self.prefix = prefix
        self.title = title
        self.singular = singular
        self.model = model
        self.name_attr = name_attr
        self._picker = picker

    def resolvable(self):
        """Every object a ref of this type may point at."""
        if self.model is ServiceBlueprint:
            # Deleting a blueprint marks it HISTORICAL rather than removing it.
            return ServiceBlueprint.objects.filter(status="ACTIVE")
        return self.model.objects.all()

    def pickable(self):
        """What the Add/Edit picker offers, mirroring the repo Contents tab."""
        qs = self._picker() if self._picker else self.resolvable()
        return qs.select_related("source_code_repo")

    def name_of(self, obj):
        return getattr(obj, self.name_attr, "") or getattr(obj, "name", "") or obj.global_id


def _standalone_plugins():
    # Shared modules subclass OrchestrationHook; they get their own section.
    return OrchestrationHook.objects.filter(shared=True).exclude(
        pk__in=SharedModule.objects.values("pk")
    )


# Picker order. Querysets follow the OOB Source Code Repository Contents tab.
MEMBER_TYPES = OrderedDict(
    (t.dir, t)
    for t in (
        MemberType("blueprints", "BP", "Blueprints", "Blueprint", ServiceBlueprint, "name"),
        MemberType("resource_actions", "RSA", "Resource actions", "Resource action", ResourceAction, "label"),
        MemberType("server_actions", "SVA", "Server actions", "Server action", ServerAction, "label"),
        MemberType(
            "orchestration_actions", "HPA", "Orchestration actions", "Orchestration action",
            HookPointAction, "name",
        ),
        MemberType("flowcontrol_actions", "FCA", "Flow control", "Flow control action", FlowControlAction, "label"),
        MemberType("recurring_jobs", "RJB", "Recurring jobs", "Recurring job", RecurringActionJob, "name"),
        MemberType("cit_tests", "CIT", "CIT tests", "CIT test", ActionCITTest, "name"),
        MemberType("webhooks", "IWH", "Webhooks", "Inbound webhook", InboundWebHook, "label"),
        MemberType("mcp_tool_actions", "MTA", "MCP tool actions", "MCP tool action", MCPToolAction, "label"),
        MemberType("shared_modules", "SHM", "Shared modules", "Shared module", SharedModule, "name"),
        MemberType(
            "plugins", "OHK", "Plugins", "Plugin", OrchestrationHook, "name", picker=_standalone_plugins
        ),
        MemberType("extensions", "XUI", "UI extensions", "UI extension", UIExtension, "label"),
    )
)


# ---------------------------------------------------------------------------
# Refs
# ---------------------------------------------------------------------------
def parse_ref(ref):
    """Split "<dir>/<ID>" into (MemberType, global_id) or raise PackageError."""
    match = REF_RE.match(ref or "") if isinstance(ref, str) else None
    if not match:
        raise PackageError(
            '"{}" is not a content reference; use "<top_level_dir>/<GLOBAL_ID>", '
            'for example "blueprints/BP-abcd1234".'.format(ref)
        )
    directory, global_id, prefix = match.group("dir"), match.group("id"), match.group("prefix")
    if directory in TRANSITIVE_DIRS:
        raise PackageError(
            '"{}": {}/ content only syncs as a dependency of its blueprint or action; '
            "add that parent instead.".format(ref, directory)
        )
    member_type = MEMBER_TYPES.get(directory)
    if member_type is None:
        raise PackageError(
            '"{}": {}/ is not a content type a package can hold. Allowed: {}.'.format(
                ref, directory, ", ".join(MEMBER_TYPES)
            )
        )
    if prefix != member_type.prefix:
        raise PackageError(
            '"{}": {}/ holds {}- IDs, not {}-.'.format(ref, directory, member_type.prefix, prefix)
        )
    return member_type, global_id


def ref_for(member_type, obj):
    return "{}/{}".format(member_type.dir, obj.global_id)


def resolve_refs(refs):
    """Map each well-formed ref to its object on this appliance, or None when
    nothing with that global ID exists. One query per member type."""
    wanted = OrderedDict()
    for ref in refs:
        try:
            member_type, global_id = parse_ref(ref)
        except PackageError:
            continue
        wanted.setdefault(member_type, []).append(global_id)
    found = {}
    for member_type, ids in wanted.items():
        for obj in member_type.resolvable().filter(global_id__in=ids).select_related("source_code_repo"):
            found[ref_for(member_type, obj)] = obj
    return {ref: found.get(ref) for ref in refs}


def validate_contents(contents, allow_unresolved=()):
    """Return contents de-duplicated in order, or raise PackageError naming
    every bad ref. Each ref must name an allowed type and resolve on this
    appliance; refs in allow_unresolved (members a package already had) may
    stay unresolved, so an edit does not force dropping a deleted member."""
    if contents is None:
        contents = []
    if not isinstance(contents, (list, tuple)):
        raise PackageError("contents must be a list of content references.")
    unique, problems = [], []
    for ref in contents:
        if ref in unique:
            continue
        try:
            parse_ref(ref)
        except PackageError as exc:
            problems.append(str(exc))
            continue
        unique.append(ref)
    resolved = resolve_refs(unique)
    for ref in unique:
        if resolved[ref] is None and ref not in allow_unresolved:
            member_type, global_id = parse_ref(ref)
            problems.append('"{}": no {} with global ID {} exists on this appliance.'.format(
                ref, member_type.singular.lower(), global_id
            ))
    if problems:
        raise PackageError(" ".join(problems))
    return unique


# ---------------------------------------------------------------------------
# Storage: one CustomFieldValue per package
# ---------------------------------------------------------------------------
def _not_found(pkg_id):
    return PackageNotFound("No content package with ID {}.".format(pkg_id))


def _existing_field():
    """The store field, or None before the first package is created."""
    return CustomField.objects.filter(name=STORE_FIELD).first()


def _field():
    """The store field, created on first use in its own Namespace."""
    namespace, _ = Namespace.objects.get_or_create(name=STORE_NAMESPACE)
    field, _ = CustomField.objects.get_or_create(
        name=STORE_FIELD,
        defaults=dict(
            label=STORE_FIELD_LABEL,
            description=STORE_FIELD_DESCRIPTION,
            type="TXT",
            namespace=namespace,
        ),
    )
    if field.type not in STORE_FIELD_TYPES:
        raise PackageError(
            "The {} parameter must be Multi-line Text or Code; it is {}.".format(STORE_FIELD, field.type)
        )
    if field.namespace_id != namespace.id:
        field.namespace = namespace
        field.save()
    return field


@contextmanager
def _locked():
    """A transaction holding a row lock on the store field, so concurrent
    writes run one at a time and see each other's results."""
    field = _field()
    with transaction.atomic():
        list(CustomField.objects.select_for_update().filter(pk=field.pk))
        yield field


def _empty_local():
    return {
        "source_code_repo_id": None,
        "branch": "",
        "remote_source_url": "",
        "last_exported": None,
        "last_synced": None,
        "last_sync_job_id": None,
    }


def _parse(text):
    package = json.loads(text or "", object_pairs_hook=OrderedDict)
    if not isinstance(package, dict) or not PKG_ID_RE.match(str(package.get("id") or "")):
        raise ValueError("not a content package")
    package.setdefault("contents", [])
    package["local"] = dict(_empty_local(), **(package.get("local") or {}))
    return package


def _rows(field):
    """[(CustomFieldValue, package)] for every stored package, oldest first.
    A value that does not parse is logged and skipped; a second value with
    an id already seen is ignored so the oldest row stays the package."""
    if field is None:
        return []
    rows, seen = [], set()
    for cfv in CustomFieldValue.objects.filter(field=field).order_by("id"):
        try:
            package = _parse(cfv.txt_value)
        except ValueError:
            logger.warning("Skipping unreadable content package value %s", cfv.id)
            continue
        if package["id"] in seen:
            logger.warning("Ignoring duplicate content package %s in value %s", package["id"], cfv.id)
            continue
        seen.add(package["id"])
        rows.append((cfv, package))
    return rows


def _find(rows, pkg_id):
    if not isinstance(pkg_id, str) or not PKG_ID_RE.match(pkg_id):
        raise _not_found(pkg_id)
    for cfv, package in rows:
        if package["id"] == pkg_id:
            return cfv, package
    raise _not_found(pkg_id)


def _store(cfv, package):
    # Set through the typed value property and save, so an existing row is
    # updated in place rather than matched or re-created by value.
    cfv.value = json.dumps(package, indent=2)
    cfv.save()


def new_package_id(existing=None):
    """PKG- plus CloudBolt's own global ID suffix, regenerated on collision."""
    if existing is None:
        existing = {p["id"] for p in list_packages()}
    while True:
        pkg_id = "{}-{}".format(ID_PREFIX, get_global_id_chars())
        if pkg_id not in existing:
            return pkg_id


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def list_packages():
    """Every stored package, sorted by name."""
    packages = [package for _, package in _rows(_existing_field())]
    return sorted(packages, key=lambda p: (p.get("name") or "").lower())


def get_package(pkg_id):
    return _find(_rows(_existing_field()), pkg_id)[1]


def _clean_fields(fields, others, current=None):
    """Validate the editable fields present in fields; return them cleaned.
    others are the stored packages to keep the name unique against."""
    unknown = sorted(set(fields) - set(EDITABLE_FIELDS))
    if unknown:
        raise PackageError("Unknown field(s): {}. Editable fields: {}.".format(
            ", ".join(unknown), ", ".join(EDITABLE_FIELDS)
        ))
    cleaned = {}
    if "name" in fields:
        name = (fields["name"] or "").strip() if isinstance(fields["name"], str) else ""
        if not name:
            raise PackageError("A package needs a name.")
        if len(name) > NAME_MAX:
            raise PackageError("Keep the name under {} characters.".format(NAME_MAX))
        for other in others:
            if (other.get("name") or "").lower() == name.lower():
                raise PackageError('A package named "{}" already exists ({}).'.format(other["name"], other["id"]))
        cleaned["name"] = name
    if "description" in fields:
        description = fields["description"] or ""
        if not isinstance(description, str):
            raise PackageError("description must be text.")
        description = " ".join(description.split())
        if len(description) > DESCRIPTION_MAX:
            raise PackageError("Keep the description under {} characters.".format(DESCRIPTION_MAX))
        cleaned["description"] = description
    if "version" in fields:
        version = (fields["version"] or "").strip() if isinstance(fields["version"], str) else ""
        if not VERSION_RE.match(version):
            raise PackageError("Version must be short and use only letters, digits, dots, plus and hyphen, "
                               "for example 1.0.0.")
        cleaned["version"] = version
    if "contents" in fields:
        existing = (current or {}).get("contents") or ()
        cleaned["contents"] = validate_contents(fields["contents"], allow_unresolved=existing)
    return cleaned


def create_package(**fields):
    fields.setdefault("version", DEFAULT_VERSION)
    fields.setdefault("description", "")
    fields.setdefault("contents", [])
    fields.setdefault("name", "")
    with _locked() as field:
        stored = [package for _, package in _rows(field)]
        cleaned = _clean_fields(fields, others=stored)
        package = OrderedDict([
            ("id", new_package_id(existing={p["id"] for p in stored})),
            ("name", cleaned["name"]),
            ("description", cleaned["description"]),
            ("version", cleaned["version"]),
            ("minimum_version_required", ""),
            ("last_updated", date.today().isoformat()),
            ("metadata_version", METADATA_VERSION),
            ("contents", cleaned["contents"]),
            ("local", _empty_local()),
        ])
        _store(CustomFieldValue(field=field), package)
    return package


def update_package(pkg_id, **fields):
    with _locked() as field:
        rows = _rows(field)
        cfv, package = _find(rows, pkg_id)
        others = [p for _, p in rows if p["id"] != pkg_id]
        cleaned = _clean_fields(fields, others=others, current=package)
        if cleaned:
            package.update(cleaned)
            package["last_updated"] = date.today().isoformat()
            _store(cfv, package)
    return package


def delete_package(pkg_id):
    """Remove the local definition only; content and repositories are untouched."""
    with _locked() as field:
        cfv, _ = _find(_rows(field), pkg_id)
        cfv.delete()
