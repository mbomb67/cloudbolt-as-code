"""Live discovery of the custom fields, actions and field dependencies behind a resource
handler's technology, plus the dependency-free .xlsx writer the export uses."""

import dis
import importlib
import io
import pkgutil
import types
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.urls import NoReverseMatch, reverse

from cbhooks.models import CloudBoltHook, OrchestrationHook
from infrastructure.models import CustomField, Environment, FieldDependency, Server
from resourcehandlers.models import ResourceHandler
from utilities.logger import ThreadLogger
from utilities.permissions import has_admin_perm_on_object

logger = ThreadLogger(__name__)


class Workbook:
    """Multi-sheet .xlsx writer. The appliance ships neither openpyxl nor xlsxwriter, so the
    OOXML parts are written by hand, strings inline (no sharedStrings table)."""

    def __init__(self):
        self.sheets = []

    @staticmethod
    def esc(value):
        """XML-escape text and drop the characters XML 1.0 forbids."""
        text = "" if value is None else str(value)
        text = "".join(c for c in text if c in ("\t", "\n", "\r") or ord(c) >= 0x20)
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def add_sheet(self, name, headers, rows):
        """Excel sheet names: at most 31 chars, none of []:*?/\\ and unique in the workbook."""
        clean = name or "Sheet"
        for ch in '[]:*?/\\':
            clean = clean.replace(ch, " ")
        clean = clean.strip()[:31] or "Sheet"
        used = {n.lower() for n, _, _ in self.sheets}
        candidate, i = clean, 1
        while candidate.lower() in used:
            suffix = f" ({i})"
            candidate = clean[:31 - len(suffix)] + suffix
            i += 1
        self.sheets.append((candidate, headers, rows))

    def to_bytes(self):
        head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        ids = range(1, len(self.sheets) + 1)
        content_types = (
            f'{head}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                      'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                      for i in ids)
            + "</Types>")
        root_rels = (
            f'{head}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>')
        sheet_tags = []
        for i, (name, _, _) in zip(ids, self.sheets):
            safe = self.esc(name).replace('"', "&quot;")
            sheet_tags.append(f'<sheet name="{safe}" sheetId="{i}" r:id="rId{i}"/>')
        workbook = (
            f'{head}<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(sheet_tags) + "</sheets></workbook>")
        workbook_rels = (
            f'{head}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" '
                      'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                      f'Target="worksheets/sheet{i}.xml"/>' for i in ids)
            + "</Relationships>")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", content_types)
            z.writestr("_rels/.rels", root_rels)
            z.writestr("xl/workbook.xml", workbook)
            z.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
            for i, (_, headers, rows) in zip(ids, self.sheets):
                out = [f'{head}<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
                for r_idx, row in enumerate([headers] + list(rows), start=1):
                    out.append(f'<row r="{r_idx}">')
                    for c_idx, val in enumerate(row):
                        col, n = "", c_idx + 1
                        while n > 0:
                            n, rem = divmod(n - 1, 26)
                            col = chr(65 + rem) + col
                        out.append(f'<c r="{col}{r_idx}" t="inlineStr"><is>'
                                   f'<t xml:space="preserve">{self.esc(val)}</t></is></c>')
                    out.append("</row>")
                out.append("</sheetData></worksheet>")
                z.writestr(f"xl/worksheets/sheet{i}.xml", "".join(out))
        return buf.getvalue()


class TechnologyReference:
    """Everything the Technology Reference tab shows for one resource handler, read live from this
    instance with nothing per-technology hardcoded, on a compiled (.pyc) appliance: fields
    from the technology's *_minimal seeds, the handler's declared parameter lists and a
    bytecode scan of its package; actions from the technology filter plus a text scan of every
    plug-in for the handler class or package name."""

    # CustomField.ATTR_TYPES codes: a dict carrying one of these as ``type`` inside a *_minimal
    # module is a field seed
    CF_TYPES = {
        "STR", "INT", "IP", "DT", "DTM", "TXT", "ETXT", "CODE", "BOOL", "DEC", "NET",
        "PWD", "TUP", "LDAP", "URL", "NSXS", "NSXE", "STOR", "FILE", "AAP",
    }
    # Framework internals that are never provisioning fields. Only a name that is NOT a real
    # custom field is rejected by this list. Value-bearing Server fields (hostname, ip, mac) are
    # deliberately absent: they are real provisioning values, shown as "server model field".
    NON_CF_NAMES = {
        "id", "pk", "real_type", "uuid", "global_id", "status", "power_status", "owner",
        "name", "environment", "nics", "resource_handler", "group",
        "resource_handler_svr_id", "tech_specific_server_info",
    }
    # custom-field accessor -> the bucket its string argument lands in
    CF_METHODS = {"get_value_for_custom_field": "used", "get_cfv_for_custom_field": "used",
                  "set_value_for_custom_field": "created"}
    # discovery key -> (sort rank, label): the evidence of HOW a field was found, not whether
    # it can be set at order time; a field is often seeded AND read, or read AND written
    DISCOVERY = {
        "seeded": (0, "Technology default"),
        "param": (1, "Declared parameter"),
        "special": (2, "Declared env field"),
        "read": (3, "Read by CloudBolt"),
        "written": (4, "Set by CloudBolt"),
        "override": (5, "Manually added"),
    }
    # model that references a hook -> (where that action runs, action_detail_by_type action_type
    # for wrappers whose own get_absolute_url() is the list page, catalog-page tab for blueprint
    # items)
    HOOK_USAGES = {
        "ServerAction": ("Server Action", "server_action", ""),
        "ResourceAction": ("Resource Action", "resource_action", ""),
        "HookPointAction": ("Orchestration Trigger", "action_trigger", ""),
        "RunCloudBoltHookServiceItem": ("Blueprint Build Item", "", "#tab-build"),
        "TearDownServiceItem": ("Blueprint Teardown", "", "#tab-teardown"),
        "RecurringActionJob": ("Recurring Job / Rule", "", ""),
    }
    # handler packages and model field lists do not change while a worker is alive
    _minimal_modules = {}
    _custom_field_names = None
    _server_field_names = None

    def __init__(self, handler, environment=None):
        self.handler = handler.cast()
        self.environment = environment
        self.rt = getattr(self.handler, "resource_technology", None)
        self.tech_name = self.rt.name if self.rt else "(no resource technology)"
        self.type_slug = getattr(self.rt, "type_slug", "") if self.rt else ""
        self.package = self.rt.modulename.rsplit(".", 1)[0] if self.rt and self.rt.modulename else ""
        self.type_name = getattr(self.handler, "type_name", self.tech_name)
        self.tech_tab = f"{self.type_name} Parameters"

    @classmethod
    def for_request(cls, request, scope, obj_id):
        """Resolve the handler behind a Resource Handler ("handler") or Environment
        ("environment") page, applying that page's own access rule (CB admin, global viewer or
        the manage permission on the object). The platform renders tab fragments and
        xui_urlpatterns without any check of its own."""
        profile = request.get_user_profile()
        environment = None
        if scope == "environment":
            environment = get_object_or_404(Environment, id=obj_id)
            handler = get_object_or_404(ResourceHandler, id=environment.resource_handler_id)
            allowed = has_admin_perm_on_object(profile, Environment, environment.id)
        else:
            handler = get_object_or_404(ResourceHandler, id=obj_id)
            allowed = has_admin_perm_on_object(profile, ResourceHandler, handler.id)
        if not (allowed or profile.global_viewer):
            raise PermissionDenied("Not authorized.")
        return cls(handler, environment)

    @classmethod
    def server_model_fields(cls):
        """Concrete value fields of the Server model (hostname, ip, mac, ...): a referenced name
        that is one of these but not a custom field is still a real provisioning value."""
        if cls._server_field_names is None:
            cls._server_field_names = {
                f.name for f in Server._meta.get_fields()
                if getattr(f, "concrete", False) and not f.is_relation
            }
        return cls._server_field_names

    def _seeded_fields(self):
        """{name: cf_dict} from the technology's *_minimal module (the fields created with the
        first handler of the technology). VMware has none: its seeds live in
        initialize.cb_minimal, which runs database queries at import time."""
        if not self.package:
            return {}
        if self.package not in self._minimal_modules:
            module = None
            try:
                pkg = importlib.import_module(self.package)
                for info in pkgutil.iter_modules(pkg.__path__):
                    if info.name.endswith("_minimal"):
                        module = importlib.import_module(f"{self.package}.{info.name}")
                        break
            except Exception as exc:
                logger.debug(f"technology_reference: minimal import failed for {self.package}: {exc}")
            self._minimal_modules[self.package] = module
        module = self._minimal_modules[self.package]
        out = {}
        for attr in dir(module) if module else []:
            value = getattr(module, attr, None)
            if not isinstance(value, list):
                continue
            for d in value:
                if (isinstance(d, dict) and isinstance(d.get("name"), str)
                        and d.get("type") in self.CF_TYPES):
                    out.setdefault(d["name"], d)
        return out

    def _scan_code(self, code, acc):
        """Record the custom-field references of one code object (and its nested ones) into
        ``acc``: used (read on a server), created, written (obj.X = ...), attr (any obj.X) and
        consts (any string literal); the caller filters attr/consts down to real fields."""
        instrs = list(dis.get_instructions(code))
        for i, ins in enumerate(instrs):
            loads_global = ins.opname in ("LOAD_GLOBAL", "LOAD_NAME", "LOAD_DEREF")
            if loads_global and ins.argval in ("hasattr", "getattr"):
                window = instrs[i + 1:i + 5]
                if any(w.opname.startswith("LOAD_FAST") and w.argval in ("server", "svr") for w in window):
                    for w in window:
                        if w.opname == "LOAD_CONST" and isinstance(w.argval, str):
                            acc["used"].add(w.argval)
                            break
            if loads_global and ins.argval == "create_custom_field":
                for w in instrs[i + 1:i + 4]:
                    if w.opname == "LOAD_CONST" and isinstance(w.argval, str):
                        acc["created"].add(w.argval)
                        break
            if ins.opname == "LOAD_ATTR":
                target = self.CF_METHODS.get(ins.argval)
                if target:
                    for w in instrs[i + 1:i + 4]:
                        if w.opname == "LOAD_CONST" and isinstance(w.argval, str):
                            acc[target].add(w.argval)
                            break
                # a field may be read off objects other than the server (osba, template, ...)
                acc["attr"].add(ins.argval)
            if ins.opname == "STORE_ATTR":
                acc["written"].add(ins.argval)
            if ins.opname == "LOAD_CONST" and isinstance(ins.argval, str):
                acc["consts"].add(ins.argval)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                self._scan_code(const, acc)
            # an all-constant list of names is compiled into a constant tuple
            elif isinstance(const, (tuple, frozenset)):
                acc["consts"].update(item for item in const if isinstance(item, str))

    def _scan_package(self):
        """Bytecode-scan the handler class and every non-test module of its package (wrapper,
        data collector, forms, ...). Only the class's own members are scanned, so another
        handler class in the same module does not donate its fields."""
        acc = {"used": set(), "created": set(), "written": set(), "attr": set(), "consts": set()}
        targets = [type(self.handler)]
        if self.package:
            try:
                pkg = importlib.import_module(self.package)
            except Exception:
                pkg = None
            for info in pkgutil.walk_packages(pkg.__path__, prefix=f"{self.package}.") if pkg else []:
                # tests, fixtures and migrations reference fields that are not part of provisioning
                if any(hint in info.name.lower() for hint in ("test", "fake", "mock", "migration", "factor")):
                    continue
                try:
                    targets.append(importlib.import_module(info.name))
                except Exception:
                    continue
        for obj in targets:
            for attr in dir(obj):
                member = getattr(obj, attr, None)
                code = getattr(member, "__code__", None)
                if code is not None:
                    try:
                        self._scan_code(code, acc)
                    except Exception:
                        pass
                elif isinstance(member, (list, tuple, set, frozenset)):
                    acc["consts"].update(x for x in member if isinstance(x, str))
                elif isinstance(member, dict):
                    for k, v in member.items():
                        if isinstance(k, str):
                            acc["consts"].add(k)
                        if isinstance(v, str):
                            acc["consts"].add(v)
        return acc

    def _field_row(self, name, cf, seed, keys):
        """One table row: live database values first, the seed/override dict for a field that
        does not exist yet. ``keys`` are the discovery methods that found the field."""
        seed = seed or {}
        if cf is None:
            kind_key = "builtin" if name in self.server_model_fields() else "missing"
            url = ""
        else:
            kind_key = "tech" if "param" in keys else "special" if "special" in keys else "param"
            try:
                url = cf.get_absolute_url() or f"/customfields/{cf.id}/"
            except Exception:
                url = f"/customfields/{cf.id}/"
        kind = {
            "tech": f"{self.type_name} parameters",
            "special": "Environment overview",
            "param": "Environment parameters",
            "builtin": "Server model field",
            "missing": "Create to enable (Admin › Parameters)",
        }[kind_key]
        return {
            "name": name,
            "label": cf.label if cf else seed.get("label", ""),
            "type": cf.get_type_display() if cf else seed.get("type", ""),
            "required": cf.required if cf else bool(seed.get("required")),
            "allow_multiple": cf.allow_multiple if cf else bool(seed.get("allow_multiple")),
            "show_on_servers": cf.show_on_servers if cf else bool(seed.get("show_on_servers")),
            "description": (cf.description if cf and cf.description else seed.get("description", "")) or "",
            "kind_key": kind_key,
            "kind": kind,
            "url": url,
            "how_found": ", ".join(self.DISCOVERY[k][1] for k in keys),
            "rank": min(self.DISCOVERY[k][0] for k in keys),
        }

    def fields(self):
        """Custom fields the technology seeds, declares, reads or writes, tagged with how each
        was found, resolved against the live database and sorted by strongest evidence, then
        name. TECHNOLOGY_REFERENCE_FIELD_OVERRIDES in customer_settings.py adds fields the scan cannot
        see (referenced only through a variable name)."""
        cls = type(self.handler)
        seeded = self._seeded_fields()
        declared = {}
        for method_name, key in (("tech_parameter_fields", "param"), ("special_fields", "special")):
            method = getattr(cls, method_name, None)
            try:
                declared[key] = set(method() or []) if method else set()
            except Exception as exc:
                logger.debug(f"technology_reference: {cls.__name__}.{method_name} failed: {exc}")
                declared[key] = set()
        acc = self._scan_package()
        if type(self)._custom_field_names is None:
            type(self)._custom_field_names = set(CustomField.objects.values_list("name", flat=True))
        universe = type(self)._custom_field_names
        allowed = universe | self.server_model_fields()

        def keep(name):
            # a real custom field is always kept; anything else must be shaped like one
            if not name.strip() or "erverinfo" in name:
                return False
            return name in universe or (
                name not in self.NON_CF_NAMES
                and "{" not in name
                and not name.startswith("_")
                and name.replace("_", "").isalnum()
            )

        # getattr / get_value_for_custom_field arguments are custom fields by definition (this
        # is how an optional field not yet in the database is found); broad hits (attribute
        # names, string literals, assignments) count only when they name a real custom field
        # or a Server model field
        by_source = {
            "seeded": set(seeded),
            "param": declared["param"],
            "special": declared["special"],
            "read": {n for n in acc["used"] | ((acc["attr"] | acc["consts"]) & allowed) if keep(n)},
            "written": {n for n in acc["created"] | (acc["written"] & allowed) if keep(n)},
        }
        names = {n for n in set().union(*by_source.values()) if n and n.strip()}
        live = {cf.name: cf for cf in CustomField.objects.filter(name__in=names)}
        rows = [
            self._field_row(name, live.get(name), seeded.get(name),
                            [k for k in self.DISCOVERY if name in by_source.get(k, ())] or ["read"])
            for name in names
        ]
        overrides = getattr(settings, "TECHNOLOGY_REFERENCE_FIELD_OVERRIDES", None) or {}
        for extra in list(overrides.get(self.type_slug, [])) + list(overrides.get("*", [])):
            name = extra.get("name")
            if name and name not in names:
                names.add(name)
                rows.append(self._field_row(name, live.get(name), extra, ["override"]))
        rows.sort(key=lambda f: (f["rank"], f["name"]))
        return rows

    def actions(self):
        """Actions for this technology: those filtered to it, plus every CloudBolt plug-in whose
        source names the handler class or package (an XaaS blueprint action usually narrows its
        environments in code rather than on the action). Each row links the action and every
        place it is used, with disabled uses marked."""
        if not self.rt:
            return []
        cls_name = type(self.handler).__name__
        found = {}
        for hook in OrchestrationHook.objects.filter(resource_technologies=self.rt).distinct():
            try:
                found[hook.id] = (hook.cast(), "Technology filter")
            except Exception:
                found[hook.id] = (hook, "Technology filter")
        needles = [cls_name.lower()] + ([self.package] if self.package else [])
        for hook in CloudBoltHook.objects.exclude(module_file="").exclude(id__in=list(found)):
            try:
                text = Path(hook.module_file.path).read_text(errors="replace").lower()
            except Exception:
                continue
            if any(n in text for n in needles):
                found[hook.id] = (hook, f"References {cls_name} in code")

        rows = []
        for hook, how in found.values():
            usage = []
            for rel in type(hook)._meta.related_objects:
                if rel.related_model.__name__ not in self.HOOK_USAGES:
                    continue
                label, detail_type, tab_anchor = self.HOOK_USAGES[rel.related_model.__name__]
                try:
                    related = list(getattr(hook, rel.get_accessor_name()).all())
                except Exception:
                    continue
                for obj in related:
                    target = getattr(obj, "blueprint", None) or obj
                    hook_point = getattr(obj, "hook_point", None)
                    url = target.get_absolute_url() if hasattr(target, "get_absolute_url") else ""
                    if url and tab_anchor:
                        url = f"{url}{tab_anchor}"
                    if detail_type:
                        try:
                            url = reverse("action_detail_by_type",
                                          kwargs={"action_type": detail_type, "action_id": obj.id})
                        except NoReverseMatch:
                            pass  # release without the detail route: keep the list page
                    usage.append({
                        "label": label,
                        "name": (hook_point.name if hook_point else None)
                                or getattr(target, "label", None) or getattr(target, "name", None) or str(target),
                        "url": url,
                        "enabled": getattr(obj, "enabled", True),
                    })
            usage.sort(key=lambda u: (u["label"], str(u["name"]).lower()))
            rows.append({
                "name": hook.name,
                "url": hook.get_absolute_url(),
                "description": (hook.description or "").strip(),
                "how_found": how,
                "hook_points": ", ".join(sorted({u["name"] for u in usage if u["label"] == "Orchestration Trigger"})),
                "usage": usage,
                "usage_text": "; ".join(
                    f"{u['label']}: {u['name']}{'' if u['enabled'] else ' (disabled)'}" for u in usage
                ) or "—",
                "module": str(getattr(hook, "module_file", "") or ""),
            })
        return sorted(rows, key=lambda r: (r["usage_text"], r["name"].lower()))

    def dependencies(self, field_names):
        """Show/hide and regenerate-options rules whose controlling field is one of these."""
        deps = FieldDependency.objects.filter(
            controlling_field__name__in=field_names
        ).select_related("dependent_field", "controlling_field")
        rows = [{
            "dependent": getattr(dep.dependent_field, "name", ""),
            "controlling": getattr(dep.controlling_field, "name", ""),
            "type": dep.dependency_type,
        } for dep in deps]
        return sorted(rows, key=lambda r: (r["controlling"], r["dependent"]))

    def context(self):
        """Template context for the tab; the export builds its sheets from the same dict."""
        fields = self.fields()
        actions = self.actions()
        deps = self.dependencies([f["name"] for f in fields])
        scope, obj_id = ("environment", self.environment.id) if self.environment else ("handler", self.handler.id)
        return {
            "handler": self.handler,
            "environment": self.environment,
            "tech_name": self.tech_name,
            "tech_tab": self.tech_tab,
            "export_url": reverse("technology_reference_export", args=[scope, obj_id]),
            "fields": fields,
            "cf_count": len(fields),
            "action_rows": actions,
            "action_count": len(actions),
            "dep_rows": deps,
            "dep_count": len(deps),
        }

    def xlsx(self):
        """The three sub-tabs as one workbook, a sheet per sub-tab."""
        ctx = self.context()

        def yes(flag):
            return "Yes" if flag else ""

        book = Workbook()
        book.add_sheet(
            "Custom Fields",
            ["Name", "Label", "Type", "How it's found", "Required", "Multi", "Show on Servers", "Kind", "Description"],
            [[f["name"], f["label"], f["type"], f["how_found"], yes(f["required"]), yes(f["allow_multiple"]),
              yes(f["show_on_servers"]), f["kind"], f["description"]] for f in ctx["fields"]])
        book.add_sheet(
            "Actions",
            ["Action", "Used in", "Trigger Points", "How it's found", "Module", "Description"],
            [[a["name"], a["usage_text"], a["hook_points"], a["how_found"], a["module"],
              a["description"]] for a in ctx["action_rows"]])
        book.add_sheet(
            "Field Dependencies",
            ["Dependent Field", "Controlling Field", "Dependency Type"],
            [[d["dependent"], d["controlling"], d["type"]] for d in ctx["dep_rows"]])
        return book.to_bytes()
