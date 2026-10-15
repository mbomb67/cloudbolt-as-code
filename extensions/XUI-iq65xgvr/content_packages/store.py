"""
Content package store

A content package is a named list of top-level content units, written as the
repo's cross-reference strings ("<top_level_dir>/<GLOBAL_ID>"). Each package
is one JSON file, <PKG-id>.json, under PROSERV_DIR/content_packages/. The file
holds the manifest that an export will write to packages/<PKG-id>/ in a
repository, plus a "local" block (home repository and branch, export and sync
history) that stays on this appliance.

Writes go to a temp file in the same directory and are renamed into place, so
a reader never sees a partial file. The directory sits outside the XUI folder,
so re-syncing or upgrading the extension leaves the definitions alone.

Member names are never stored: they are resolved from the appliance on every
read, so the page can flag members that were deleted or that live on another
repository or branch.
"""
import json
import os
import re
import tempfile
from collections import OrderedDict
from datetime import date

from django.conf import settings

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
from servicecatalog.models import ServiceBlueprint
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

STORE_DIR = os.path.join(settings.PROSERV_DIR, "content_packages")

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
# Files
# ---------------------------------------------------------------------------
def _path(pkg_id):
    if not isinstance(pkg_id, str) or not PKG_ID_RE.match(pkg_id):
        raise PackageNotFound("No content package with ID {}.".format(pkg_id))
    return os.path.join(STORE_DIR, pkg_id + ".json")


def _write(package):
    os.makedirs(STORE_DIR, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=STORE_DIR, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(package, stream, indent=4)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, _path(package["id"]))
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def _read(path):
    with open(path) as stream:
        package = json.load(stream)
    package.setdefault("contents", [])
    package["local"] = dict(_empty_local(), **(package.get("local") or {}))
    return package


def _empty_local():
    return {
        "source_code_repo_id": None,
        "branch": "",
        "remote_source_url": "",
        "last_exported": None,
        "last_synced": None,
        "last_sync_job_id": None,
    }


def new_package_id():
    """PKG- plus CloudBolt's own global ID suffix, regenerated on collision."""
    while True:
        pkg_id = "{}-{}".format(ID_PREFIX, get_global_id_chars())
        if not os.path.exists(_path(pkg_id)):
            return pkg_id


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def list_packages():
    """Every stored package, sorted by name. A file that does not parse is
    logged and skipped so one bad file does not hide the rest."""
    if not os.path.isdir(STORE_DIR):
        return []
    packages = []
    for filename in sorted(os.listdir(STORE_DIR)):
        pkg_id, ext = os.path.splitext(filename)
        if ext != ".json" or not PKG_ID_RE.match(pkg_id):
            continue
        try:
            packages.append(_read(os.path.join(STORE_DIR, filename)))
        except (OSError, ValueError):
            logger.exception("Skipping unreadable content package file %s", filename)
    return sorted(packages, key=lambda p: (p.get("name") or "").lower())


def get_package(pkg_id):
    path = _path(pkg_id)
    if not os.path.exists(path):
        raise PackageNotFound("No content package with ID {}.".format(pkg_id))
    return _read(path)


def _clean_fields(fields, current=None):
    """Validate the editable fields present in fields; return them cleaned."""
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
        for other in list_packages():
            if other["id"] != (current or {}).get("id") and (other.get("name") or "").lower() == name.lower():
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
    cleaned = _clean_fields(fields)
    package = OrderedDict([
        ("id", new_package_id()),
        ("name", cleaned["name"]),
        ("description", cleaned["description"]),
        ("version", cleaned["version"]),
        ("minimum_version_required", ""),
        ("last_updated", date.today().isoformat()),
        ("metadata_version", METADATA_VERSION),
        ("contents", cleaned["contents"]),
        ("local", _empty_local()),
    ])
    _write(package)
    return package


def update_package(pkg_id, **fields):
    package = get_package(pkg_id)
    cleaned = _clean_fields(fields, current=package)
    if cleaned:
        package.update(cleaned)
        package["last_updated"] = date.today().isoformat()
        _write(package)
    return package


def delete_package(pkg_id):
    """Remove the local definition only; content and repositories are untouched."""
    path = _path(pkg_id)
    if not os.path.exists(path):
        raise PackageNotFound("No content package with ID {}.".format(pkg_id))
    os.remove(path)
