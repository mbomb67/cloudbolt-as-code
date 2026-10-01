"""Package content from this repo into a zip that CloudBolt's Import button accepts.

This repo uses CloudBolt's Source Control Repos layout: one ID-named folder per
unit, cross-references as "<dir>/<ID>" strings under `dependencies`. CloudBolt's
zip import/export uses a different shape: one `<slug>/<slug>.json` package per
unit, dependencies nested as inner zips with prefixed names, and no
`dependencies` or `metadata_version` keys. This tool converts the first shape
into the second, following the unit's dependencies transitively, and writes one
uploadable zip. The format is documented in docs/agents/zip-package-format.md.

  python tools/package_zip.py BP-5pei9cno                # by ID
  python tools/package_zip.py blueprints/BP-5pei9cno     # by repo path
  python tools/package_zip.py "Azure Subscription"       # by name or label
  python tools/package_zip.py BP-5pei9cno --dry-run      # print the planned tree only
  python tools/package_zip.py BP-5pei9cno --out ./build  # default: dist/

Exit status: 0 packaged, 1 cannot package (missing file, unsupported content,
broken reference), 2 target not found or ambiguous.

No dependencies. Run from the repo root.
"""
import argparse
import copy
import io
import json
import os
import re
import sys
import unicodedata
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_catalog import CONTENT_DIRS, load_units  # noqa: E402

ACTION_DIRS = (
    "resource_actions", "server_actions", "orchestration_actions", "flowcontrol_actions",
    "recurring_jobs", "cit_tests", "webhooks", "mcp_tool_actions",
)
# Keys that exist only in the repo layout. CloudBolt's zip importer ignores
# `dependencies` and `has_custom_form`, but a present `metadata_version` makes it
# treat the zip as the flattened format and skip every nested zip.
REPO_ONLY_KEYS = ("dependencies", "metadata_version", "has_custom_form")
# Deployment item kinds whose nested payload is a Terraform working directory or a
# provider object, which this repo layout does not carry. Export those from CloudBolt.
UNSUPPORTED_TIERS = ("tfconfig", "tfoperation", "terraform", "pod", "loadbalancer", "network")
PLACEHOLDERS = ("YOUR_CREDENTIALS", "YOUR_AUTH_INFO", "YOUR_EMAIL_INFO", "SOURCE_CODE_URL Redacted")
SECRET_INPUT_TYPES = ("PWD", "ETXT")
# Where the finished zip goes, per content type (CloudBolt v2026.3). "UI" is the
# upload dialog on that admin page; "API" is a multipart POST with zipFile=@file
# and optional replaceExisting=true.
UPLOAD_ROUTES = {
    "blueprints": "UI: Blueprints list > Upload; API: POST /api/v3/cmp/blueprints/",
    "orchestration_actions": "UI: Admin > Orchestration Actions > Upload; API: POST /api/v3/cmp/orchestrationActions/",
    "resource_actions": "UI: Admin > Resource Actions > Upload; API: POST /api/v3/cmp/resourceActions/",
    "server_actions": "UI: Admin > Server Actions > Upload; API: POST /api/v3/cmp/serverActions/",
    "recurring_jobs": "UI: Admin > Recurring Jobs > Upload; API: POST /api/v3/cmp/scheduledActions/",
    "webhooks": "UI: Admin > Inbound Web Hooks > Upload; API: POST /api/v3/cmp/inboundWebHooks/",
    "mcp_tool_actions": "UI: Admin > MCP Tool Actions > Upload; API: POST /api/v3/cmp/mcpToolActions/",
    "flowcontrol_actions": "API only: POST /api/v3/cmp/flowControlActions/ (no UI upload)",
    "plugins": "API only: POST /api/v3/cmp/actions/ (no UI upload for a bare plugin; or Content Library)",
    "shared_modules": "UI only: Admin > Shared Modules > Upload (no API route)",
    "extensions": "UI: Admin > UI Extensions > Upload; API: POST /api/v3/cmp/uiExtensions/",
}


class Problem(Exception):
    """The content cannot be packaged as requested."""


def slugify(value):
    """Django's slugify: ASCII, lowercase, non-word characters dropped, runs of space/hyphen to '-'."""
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value.lower())
    return re.sub(r"[-\s]+", "-", value).strip("-_")


def pkg_slug(name):
    """CloudBolt's package name: slugify, then hyphens to underscores."""
    return slugify(name).replace("-", "_")


def dump_json(obj):
    """Match CloudBolt's own export formatting."""
    return (json.dumps(obj, indent=4, sort_keys=True, separators=(",", ": "), ensure_ascii=False) + "\n").encode("utf-8")


def export_meta(meta):
    """Deep copy with the repo-only keys removed at every depth."""
    def strip(obj):
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items() if k not in REPO_ONLY_KEYS}
        if isinstance(obj, list):
            return [strip(v) for v in obj]
        return obj
    return strip(copy.deepcopy(meta))


def find_placeholders(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from find_placeholders(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from find_placeholders(v, f"{path}[{i}]")
    elif isinstance(obj, str) and obj in PLACEHOLDERS:
        yield path, obj


class RawZip:
    """A nested zip that is not a package (the XUI file tree)."""

    def __init__(self, members):
        self.members = members  # [(arcname, bytes)]

    def to_bytes(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, data in self.members:
                zf.writestr(arcname, data)
        return buf.getvalue()

    def tree(self, indent):
        pad = " " * indent
        return "".join(f"{pad}{arc}\n" for arc, _ in self.members)


class Package:
    """One CloudBolt package: folder `slug/` holding `slug.json` plus sibling files."""

    def __init__(self, slug, meta, unit):
        self.slug = slug
        self.meta = meta
        self.unit = unit
        self.files = []  # [(basename, bytes | Package | RawZip)]

    def add(self, name, payload):
        if any(n == name for n, _ in self.files) or name == f"{self.slug}.json":
            raise Problem(f"{self.unit.path}: two members would be named {name!r} inside package {self.slug}/; "
                          "CloudBolt keeps only one. Rename one of the units.")
        self.files.append((name, payload))

    def member_names(self):
        return [n for n, _ in self.files]

    def to_bytes(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{self.slug}/{self.slug}.json", dump_json(self.meta))
            for name, payload in self.files:
                data = payload if isinstance(payload, bytes) else payload.to_bytes()
                zf.writestr(f"{self.slug}/{name}", data)
        return buf.getvalue()

    def tree(self, indent=0):
        pad = " " * indent
        out = f"{pad}{self.slug}/{self.slug}.json  ({self.unit.path})\n"
        for name, payload in self.files:
            out += f"{pad}{self.slug}/{name}\n"
            if not isinstance(payload, bytes):
                out += payload.tree(indent + 4)
        return out

    def walk(self):
        """Yield every package in this tree, self first."""
        yield self
        for _, payload in self.files:
            if isinstance(payload, Package):
                yield from payload.walk()


class Packager:
    def __init__(self, units):
        self.units = units
        self.warnings = []
        self.after_import = []

    # -- helpers -------------------------------------------------------------

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def note(self, msg):
        if msg not in self.after_import:
            self.after_import.append(msg)

    def dep(self, ref, from_unit, key):
        if not isinstance(ref, str):
            raise Problem(f"{from_unit.path}: {key} must be a \"<dir>/<ID>\" string, got {ref!r}")
        unit = self.units.get(ref)
        if unit is None:
            raise Problem(f"{from_unit.path}: {key} = {ref!r} does not exist in the repo")
        return unit

    def read(self, unit, filename, key):
        path = os.path.join(unit.dir, unit.id, filename)
        if not os.path.isfile(path):
            raise Problem(f"{unit.path}: {key} names {filename!r} but the file is missing")
        with open(path, "rb") as fh:
            return fh.read()

    def read_optional(self, unit, filename):
        path = os.path.join(unit.dir, unit.id, filename or "")
        if filename and os.path.isfile(path):
            with open(path, "rb") as fh:
                return fh.read()
        return None

    def importable_parents(self, unit, seen=None):
        """Blueprints/actions that transitively reference a form or form function."""
        seen = seen if seen is not None else set()
        found = []
        for ref in unit.used_by:
            if ref in seen:
                continue
            seen.add(ref)
            parent = self.units[ref]
            if parent.dir in ("forms", "form_functions"):
                found.extend(self.importable_parents(parent, seen))
            else:
                found.append(ref)
        return sorted(set(found))

    def record_placeholders(self, unit):
        for path, value in find_placeholders(unit.meta):
            self.note(f"{unit.path}: {path} is the redaction placeholder {value!r}; re-enter the real value in CloudBolt after import.")

    # -- dispatch ------------------------------------------------------------

    def package(self, unit):
        if unit.dir == "blueprints":
            return self.pack_blueprint(unit)
        if unit.dir == "plugins":
            return self.pack_plugin(unit)
        if unit.dir == "shared_modules":
            return self.pack_shared_module(unit)
        if unit.dir in ACTION_DIRS:
            return self.pack_action(unit)
        if unit.dir == "extensions":
            return self.pack_extension(unit)
        if unit.dir in ("forms", "form_functions"):
            parents = self.importable_parents(unit)
            hint = ", ".join(parents) if parents else "nothing in the repo references it"
            raise Problem(f"{unit.path}: CloudBolt has no standalone import for {unit.dir}; package the parent instead ({hint})")
        raise Problem(f"{unit.path}: unsupported content type {unit.dir!r}")

    # -- plugins and shared modules -----------------------------------------

    def pack_plugin(self, unit, extra_shared_modules=()):
        src = unit.meta
        meta = export_meta(src)
        self.record_placeholders(unit)
        pkg = Package(pkg_slug(src["name"]), meta, unit)
        script = src.get("script_filename")
        if script:
            pkg.add(script, self.read(unit, script, "script_filename"))
        elif not src.get("source_code_url"):
            raise Problem(f"{unit.path}: plugin has neither script_filename nor source_code_url")
        self.add_shared_modules(unit, pkg, extra_shared_modules)
        for i, inp in enumerate(src.get("action_inputs") or []):
            out = meta["action_inputs"][i]
            # The importer reads formatter_pattern and overwrites value_pattern_string with it.
            if inp.get("value_pattern_string") and not inp.get("formatter_pattern"):
                out["formatter_pattern"] = inp["value_pattern_string"]
            self.gen_options_for(unit, pkg, inp, out, f"action_inputs[{i}]")
        return pkg

    def gen_options_for(self, unit, pkg, src_holder, out_holder, where):
        """Package generated-options actions a parameter declares; drop declarations that have no action.

        CloudBolt resolves every gen_options_hooks[].name against the genoptions_*.zip
        members and raises if one is missing, so an unresolvable entry must not ship.
        """
        kept = []
        for j, goh in enumerate(src_holder.get("gen_options_hooks") or []):
            ref = (goh.get("dependencies") or {}).get("orchestration_hook")
            if ref:
                self.add_gen_options(unit, pkg, goh, ref, f"{where}.gen_options_hooks[{j}]")
                kept.append({k: v for k, v in goh.items() if k != "dependencies"})
            else:
                self.warn(f"{unit.path}: {where}.gen_options_hooks[{j}] ({goh.get('name')!r}) has no dependencies.orchestration_hook; dropped so the import does not fail.")
        if "gen_options_hooks" in out_holder:
            out_holder["gen_options_hooks"] = kept

    def add_shared_modules(self, unit, pkg, extra=()):
        refs = list((unit.meta.get("dependencies") or {}).get("sharedModules") or []) + list(extra)
        names = []
        for ref in refs:
            shm = self.dep(ref, unit, "dependencies.sharedModules")
            sp = self.pack_shared_module(shm)
            name = f"shared_module_{sp.slug}.zip"
            if name not in names:
                pkg.add(name, sp)
                names.append(name)
        if names:
            pkg.meta["shared_module_dependencies"] = names

    def pack_shared_module(self, unit):
        src = unit.meta
        meta = export_meta(src)
        self.record_placeholders(unit)
        module = src.get("module_name") or src["name"]
        script = src.get("script_filename")
        if not script:
            raise Problem(f"{unit.path}: shared module has no script_filename")
        data = self.read(unit, script, "script_filename")
        # CloudBolt's own export names the file after the module (ldap_dns/ldap_dns.py).
        meta["script_filename"] = f"{module}.py"
        meta.setdefault("module_name", module)
        pkg = Package(pkg_slug(module), meta, unit)
        pkg.add(f"{module}.py", data)
        self.add_shared_modules(unit, pkg)
        return pkg

    # -- actions wrapping a plugin ------------------------------------------

    def pack_action(self, unit):
        src = unit.meta
        deps = src.get("dependencies") or {}
        meta = export_meta(src)
        self.record_placeholders(unit)
        hook_ref = deps.get("hook")
        if not hook_ref:
            raise Problem(f"{unit.path}: dependencies.hook is required to package an action")
        hook = self.dep(hook_ref, unit, "dependencies.hook")
        # Action-level shared modules ride inside the plugin zip, which is what imports them.
        hook_pkg = self.pack_plugin(hook, extra_shared_modules=deps.get("sharedModules") or [])
        meta.setdefault("base_action_name", hook.meta.get("name"))
        # CloudBolt names the package after `name` for types that have one, else `label`.
        if unit.dir in ("orchestration_actions", "recurring_jobs", "cit_tests"):
            title = src.get("name") or src.get("label") or hook.meta["name"]
        else:
            title = src.get("label") or src.get("name") or hook.meta["name"]
        pkg = Package(pkg_slug(title), meta, unit)
        # The importer takes the first non-JSON member as the plugin zip, so it goes first.
        pkg.add(f"{hook_pkg.slug}.zip", hook_pkg)
        if deps.get("displayCondition"):
            cond = self.pack_plugin(self.dep(deps["displayCondition"], unit, "dependencies.displayCondition"))
            pkg.add(f"condition_{cond.slug}.zip", cond)
        if deps.get("custom_form"):
            form = self.dep(deps["custom_form"], unit, "dependencies.custom_form")
            form_pkg = self.pack_form(form, title)
            pkg.add(f"{form_pkg.slug}.zip", form_pkg)
        self.fix_default_values(unit, meta, hook, "action_input_default_values")
        if unit.dir == "orchestration_actions":
            self.note(f"{unit.path}: hook point {src.get('hook_point')!r} must already exist on the target CloudBolt, or the import falls back to the legacy serializer.")
        if unit.dir == "recurring_jobs":
            if src.get("type") != "orchestration_hook":
                raise Problem(f"{unit.path}: type must be \"orchestration_hook\" for CloudBolt to attach the plugin")
            self.note(f"{unit.path}: CloudBolt ignores enabled and allow_parallel_jobs on import (enabled follows the auto-enable preference); check them after import.")
        if unit.dir == "mcp_tool_actions":
            self.note(f"{unit.path}: import fails if another MCP tool action already uses mcp_tool_name {src.get('mcp_tool_name')!r}.")
        if unit.dir == "cit_tests":
            raise Problem(f"{unit.path}: CloudBolt has no zip import for CIT tests (no UI dialog, no API route); sync cit_tests/ from the repo instead")
        return pkg

    def input_types(self, hook):
        """Map of input name (no _a<n> suffix) -> CloudBolt field type for a plugin."""
        return {str(i.get("name")): str(i.get("type") or "STR") for i in hook.meta.get("action_inputs") or [] if isinstance(i, dict)}

    def fix_default_values(self, unit, holder, hook, key):
        """Default values are matched to inputs by the name's prefix before its last '_' segment."""
        types = self.input_types(hook)
        for dv in holder.get(key) or []:
            name = str(dv.get("name") or "")
            if not re.search(r"_a\d+$", name):
                dv["name"] = name + "_a0"
            base = re.sub(r"_a\d+$", "", dv["name"])
            if types.get(base) in SECRET_INPUT_TYPES:
                self.note(f"{unit.path}: {key} for {base!r} is a {types[base]} value encrypted for the source instance; re-enter it in CloudBolt after import.")

    def add_gen_options(self, unit, pkg, goh, ref, key):
        hpa = self.dep(ref, unit, f"{key}.dependencies.orchestration_hook")
        if hpa.dir != "orchestration_actions":
            raise Problem(f"{unit.path}: {key} must reference orchestration_actions/HPA-*, got {ref!r}")
        hpa_pkg = self.pack_action(hpa)
        wanted = goh.get("name") or ""
        if not str(hpa.meta.get("name", "")).startswith(wanted):
            self.warn(f"{unit.path}: {key}.name {wanted!r} is not a prefix of the action name {hpa.meta.get('name')!r}; "
                      "CloudBolt matches generated-options actions by name prefix and will fail the import.")
        name = f"genoptions_{hpa_pkg.slug}.zip"
        if name not in pkg.member_names():
            pkg.add(name, hpa_pkg)

    # -- forms ---------------------------------------------------------------

    def pack_form(self, unit, parent_name):
        src = unit.meta
        deps = src.get("dependencies") or {}
        meta = export_meta(src)
        meta.pop("rendering_mode", None)  # field no longer exists in CloudBolt
        if "json" not in src:
            raise Problem(f"{unit.path}: form metadata has no json field")
        if not isinstance(src["json"], str):
            meta["json"] = json.dumps(src["json"])  # CloudBolt stores and re-parses it as a string
        pkg = Package(f"custom_form_{pkg_slug(parent_name)}", meta, unit)
        css = src.get("css_file")
        if css:
            pkg.add(css, self.read(unit, css, "css_file"))
        else:
            meta.pop("css_file", None)
        names = []
        for ref in deps.get("form_functions") or []:
            fjs = self.dep(ref, unit, "dependencies.form_functions")
            fp = self.pack_form_function(fjs)
            name = f"form_function_{fp.slug}.zip"
            pkg.add(name, fp)
            names.append(name)
        meta["functions"] = names
        return pkg

    def pack_form_function(self, unit):
        meta = export_meta(unit.meta)
        for key in ("id", "name", "code"):
            if key not in meta:
                raise Problem(f"{unit.path}: form function metadata has no {key} field")
        return Package(pkg_slug(unit.id), meta, unit)

    # -- blueprints ----------------------------------------------------------

    def pack_blueprint(self, unit):
        src = unit.meta
        deps = src.get("dependencies") or {}
        meta = export_meta(src)
        self.record_placeholders(unit)
        meta.setdefault("deployment_items", [])
        meta.setdefault("teardown_items", [])
        pkg = Package(pkg_slug(src["name"]), meta, unit)
        prefixes = {}  # member-name prefix the importer searches for -> member

        def add_prefixed(prefix, payload, exact):
            # CloudBolt picks nested zips by prefix and silently keeps the last match.
            if prefix in prefixes:
                raise Problem(f"{unit.path}: two nested zips would both match prefix {prefix!r} "
                              f"({prefixes[prefix]} and {exact}); give the items distinct deploy_seq values.")
            prefixes[prefix] = exact
            pkg.add(exact, payload)

        # Images: the list image, plus any label/category icons that happen to be in the folder.
        icon = src.get("icon")
        if icon:
            data = self.read_optional(unit, icon)
            if data is None:
                self.warn(f"{unit.path}: icon {icon!r} is not in the folder; the blueprint imports without an image.")
                meta.pop("icon", None)
            else:
                pkg.add(icon, data)
        for label in src.get("labels") or []:
            node = label
            while isinstance(node, dict):
                data = self.read_optional(unit, node.get("icon"))
                if data is not None and node["icon"] not in pkg.member_names():
                    pkg.add(node["icon"], data)
                node = node.get("parent")

        for list_key, prefix in (("deployment_items", "build"), ("teardown_items", "teardown")):
            for i, item in enumerate(src.get(list_key) or []):
                where = f"{list_key}[{i}]"
                out_item = meta[list_key][i]
                seq = item.get("deploy_seq")
                tier = item.get("tier_type")
                item_deps = item.get("dependencies") or {}
                if tier in UNSUPPORTED_TIERS or (tier or "").startswith("teardown_") and tier[len("teardown_"):] in UNSUPPORTED_TIERS:
                    raise Problem(f"{unit.path}: {where} is a {tier!r} item; its payload is not in the repo layout, so export that blueprint from CloudBolt instead")
                if seq is None:
                    raise Problem(f"{unit.path}: {where} has no deploy_seq; CloudBolt locates its plugin zip by deploy_seq")
                if "hook" in item_deps:
                    hp = self.pack_plugin(self.dep(item_deps["hook"], unit, f"{where}.dependencies.hook"))
                    add_prefixed(f"{prefix}_{seq}_", hp, f"{prefix}_{seq}_{hp.slug}.zip")
                    out_item.setdefault("action_name", hp.unit.meta.get("name"))
                    self.fix_default_values(unit, out_item, hp.unit, "parameter_defaults")
                elif tier not in ("server", "blueprint"):
                    self.warn(f"{unit.path}: {where} ({tier}) has no dependencies.hook; it imports without a plugin.")
                if "blueprint" in item_deps:
                    sub = self.dep(item_deps["blueprint"], unit, f"{where}.dependencies.blueprint")
                    if sub.path == unit.path:
                        raise Problem(f"{unit.path}: {where} references the blueprint itself")
                    sp = self.pack_blueprint(sub)
                    # Sub-blueprints are matched on "build_<seq>" with no trailing underscore.
                    add_prefixed(f"build_{seq}", sp, f"build_{seq}_{sp.slug}.zip")
                if "rate_hook" in item_deps:
                    rp = self.pack_plugin(self.dep(item_deps["rate_hook"], unit, f"{where}.dependencies.rate_hook"))
                    add_prefixed(f"ratehook_{seq}_", rp, f"ratehook_{seq}_{rp.slug}.zip")
                    out_item["rate_action_name"] = rp.unit.meta.get("name")
                if "environment_selection_hook" in item_deps:
                    ep = self.pack_plugin(self.dep(item_deps["environment_selection_hook"], unit, f"{where}.dependencies.environment_selection_hook"))
                    add_prefixed(f"environment_selection_{seq}_", ep, f"environment_selection_{seq}_{ep.slug}.zip")
                    out_item["environment_selection_orchestration"] = {"title": ep.unit.meta.get("name")}
                if "jobengine_selection_hook" in item_deps:
                    self.warn(f"{unit.path}: {where}.dependencies.jobengine_selection_hook has no zip equivalent; re-select the job engine hook in CloudBolt after import.")
                os_build = item.get("os_build") or {}
                if isinstance(os_build, dict) and os_build.get("title"):
                    self.note(f"{unit.path}: {where} needs an OS build named {os_build['title']!r} on the target CloudBolt (matched by name, silently skipped if absent).")

        # Ambiguity check for sub-blueprint prefixes: build_1 also matches build_10_...
        for prefix, exact in list(prefixes.items()):
            if re.fullmatch(r"build_-?\d+", prefix):
                clashes = [m for m in pkg.member_names() if m.startswith(prefix) and m != exact]
                if clashes:
                    raise Problem(f"{unit.path}: sub-blueprint member {exact} is matched on prefix {prefix!r}, which also matches {clashes}; renumber deploy_seq.")

        disc = src.get("discovery_plugin") or {}
        if (disc.get("dependencies") or {}).get("hook"):
            dp = self.pack_plugin(self.dep(disc["dependencies"]["hook"], unit, "discovery_plugin.dependencies.hook"))
            add_prefixed("discovery_", dp, f"discovery_{dp.slug}.zip")
            meta["discovery_plugin"] = {"title": dp.unit.meta.get("name")}

        for i, ma in enumerate(src.get("management_actions") or []):
            ref = (ma.get("dependencies") or {}).get("resource_action")
            if not ref:
                self.warn(f"{unit.path}: management_actions[{i}] has no dependencies.resource_action; it is skipped.")
                continue
            rsa = self.dep(ref, unit, f"management_actions[{i}].dependencies.resource_action")
            if rsa.dir != "resource_actions":
                raise Problem(f"{unit.path}: management_actions[{i}] must reference resource_actions/RSA-*, got {ref!r}")
            rp = self.pack_action(rsa)
            pkg.add(f"management_{rp.slug}.zip", rp)
            meta["management_actions"][i].setdefault("label", rsa.meta.get("label"))

        for i, param in enumerate(src.get("parameters") or []):
            self.gen_options_for(unit, pkg, param, meta["parameters"][i], f"parameters[{i}]")
        meta["labels"] = [lb for lb in meta.get("labels") or [] if lb]

        if deps.get("custom_form"):
            form = self.dep(deps["custom_form"], unit, "dependencies.custom_form")
            fp = self.pack_form(form, src["name"])
            pkg.add(f"{fp.slug}.zip", fp)
            meta["custom_form"] = form.id

        self.note(f"{unit.path}: if the target already has this blueprint as a Source Control Repos sync, do not use "
                  "replaceExisting: CloudBolt would take its repo-refresh path and look for folders in the clone instead of "
                  "the nested zips. Import without replace (creates \"<name> (2)\") or remove the synced copy first.")
        return pkg

    # -- UI extensions -------------------------------------------------------

    def pack_extension(self, unit):
        src = unit.meta
        meta = export_meta(src)
        name = src.get("name")
        if not name:
            raise Problem(f"{unit.path}: extension metadata has no name")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise Problem(f"{unit.path}: name {name!r} must be a Python identifier (imported as xui.{name})")
        folder = os.path.join(unit.dir, unit.id, name)
        contents, seen = [], set()
        for rel in src.get("package_contents") or []:
            rel = rel.replace("\\", "/")
            if rel in seen:
                continue
            seen.add(rel)
            if os.path.isfile(os.path.join(folder, rel)):
                contents.append(rel)
            else:
                self.warn(f"{unit.path}: package_contents lists {rel!r} but {name}/{rel} is not on disk; left out.")
        if not contents:
            raise Problem(f"{unit.path}: no package files found under {folder}")
        members = []
        for rel in contents:
            with open(os.path.join(folder, rel), "rb") as fh:
                members.append((f"{name}/{rel}", fh.read()))
        meta["package_contents"] = contents
        pkg = Package(pkg_slug(name), meta, unit)
        pkg.add(f"{name}.zip", RawZip(members))
        icon = src.get("icon")
        data = self.read_optional(unit, icon)
        if data is not None:
            pkg.add(icon, data)
            meta["icon"] = icon
        else:
            meta.pop("icon", None)
        if src.get("icon_url"):
            meta.pop("icon_url", None)
        if src.get("enabled") is False:
            self.warn(f"{unit.path}: enabled=false is ignored by the importer; disable the extension in CloudBolt after import.")
        self.note(f"{unit.path}: restart the CloudBolt web server after importing a UI extension so its Python loads.")
        return pkg


# -- verification of the produced zip ----------------------------------------

PREFIX_FOR_DIR = {
    "blueprints": "BP-", "plugins": "OHK-", "shared_modules": "SHM-", "resource_actions": "RSA-",
    "server_actions": "SVA-", "orchestration_actions": "HPA-", "flowcontrol_actions": "FCA-",
    "recurring_jobs": "RJB-", "cit_tests": "CIT-", "webhooks": "IWH-", "mcp_tool_actions": "MTA-",
    "extensions": "XUI-", "forms": "FRM-", "form_functions": "FJS-",
}


def verify(data, label="root"):
    """Re-open the zip the way CloudBolt's importer does and check its invariants."""
    errors = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        jsons = [i for i in infos if i.filename.endswith(".json")]
        if len(jsons) != 1:
            errors.append(f"{label}: {len(jsons)} .json members; CloudBolt needs exactly one per zip layer")
            return errors
        meta = json.loads(zf.read(jsons[0]))
        basenames = [os.path.basename(i.filename) for i in infos if i is not jsons[0]]
        dupes = sorted({b for b in basenames if basenames.count(b) > 1})
        if dupes:
            errors.append(f"{label}: duplicate member basenames {dupes}; CloudBolt keys members by basename")
        if "metadata_version" in meta:
            errors.append(f"{label}: metadata_version present; CloudBolt would skip every nested zip")
        if "dependencies" in json.dumps(meta):
            pass  # harmless if nested somewhere unexpected; the strip is total, so this never fires
        uid = meta.get("id", "")
        if not re.match(r"^[A-Z]+-[0-9a-z]{8,12}$", uid or ""):
            errors.append(f"{label}: id {uid!r} is not a CloudBolt global ID")
        script = meta.get("script_filename")
        if script and script not in basenames and not meta.get("source_code_url"):
            errors.append(f"{label}: script_filename {script!r} is not a member")
        for key in ("shared_module_dependencies", "functions"):
            for name in meta.get(key) or []:
                if name not in basenames:
                    errors.append(f"{label}: {key} lists {name!r} which is not a member")
        opaque = {f"{meta.get('name')}.zip"} if uid.startswith("XUI-") else set()
        for info in infos:
            if info.filename.endswith(".zip"):
                inner = zf.read(info)
                base = os.path.basename(info.filename)
                if base in opaque:
                    continue
                if zipfile.is_zipfile(io.BytesIO(inner)):
                    with zipfile.ZipFile(io.BytesIO(inner)) as izf:
                        has_json = any(n.endswith(".json") for n in izf.namelist())
                    if has_json:
                        errors.extend(verify(inner, f"{label} > {base}"))
                else:
                    errors.append(f"{label}: {base} is not a zip")
    return errors


# -- target resolution ----------------------------------------------------------

def resolve(units, target):
    t = target.strip().replace("\\", "/").strip("/")
    if t.endswith("_metadata.json"):
        t = t.rsplit("/", 1)[0]
    parts = t.split("/")
    if len(parts) >= 2 and parts[-2] in CONTENT_DIRS:
        t = "/".join(parts[-2:])
    if t in units:
        return units[t]
    by_id = [u for u in units.values() if u.id == t]
    if len(by_id) == 1:
        return by_id[0]
    low = t.lower()
    exact = [u for u in units.values() if low in {str(u.meta.get("name", "")).lower(), str(u.meta.get("label", "")).lower(), u.name.lower()}]
    if len(exact) == 1:
        return exact[0]
    loose = exact or [u for u in units.values() if low in u.name.lower() or low in str(u.meta.get("name", "")).lower()]
    if len(loose) == 1:
        return loose[0]
    if not loose:
        raise LookupError(f"nothing in the repo matches {target!r}")
    lines = "\n".join(f"  {u.path}  {u.name}" for u in sorted(loose, key=lambda u: u.path))
    raise LookupError(f"{target!r} is ambiguous; pass one of:\n{lines}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target", nargs="+", help="repo path (blueprints/BP-x), global ID (BP-x), or name/label")
    ap.add_argument("--out", default="dist", help="output directory (default: dist/)")
    ap.add_argument("--dry-run", action="store_true", help="print the planned zip tree and notes; write nothing")
    args = ap.parse_args(argv)

    if not os.path.isdir("blueprints") and not os.path.isdir("plugins"):
        print("Run from the repo root (no blueprints/ or plugins/ here).", file=sys.stderr)
        return 1
    units = load_units()
    status = 0
    for target in args.target:
        try:
            unit = resolve(units, target)
        except LookupError as err:
            print(f"ERROR: {err}", file=sys.stderr)
            status = max(status, 2)
            continue
        packager = Packager(units)
        try:
            pkg = packager.package(unit)
        except Problem as err:
            print(f"ERROR: {err}", file=sys.stderr)
            status = max(status, 1)
            continue
        out_path = os.path.join(args.out, f"{pkg.slug}.zip")
        print(f"{unit.path}  {unit.name}\n  -> {out_path}{' (dry run)' if args.dry_run else ''}\n")
        print(pkg.tree(2))
        included = sorted({p.unit.path for p in pkg.walk()} - {unit.path})
        if included:
            print("  Bundled dependencies:\n" + "".join(f"    {p}\n" for p in included))
        data = pkg.to_bytes()
        problems = verify(data)
        if problems:
            print("  Verification FAILED:\n" + "".join(f"    {p}\n" for p in problems), file=sys.stderr)
            status = max(status, 1)
            continue
        if packager.warnings:
            print("  Warnings:\n" + "".join(f"    - {w}\n" for w in packager.warnings))
        if packager.after_import:
            print("  After import:\n" + "".join(f"    - {n}\n" for n in packager.after_import))
        print(f"  Upload via: {UPLOAD_ROUTES.get(unit.dir, 'see docs/agents/zip-package-format.md')}\n")
        if not args.dry_run:
            os.makedirs(args.out, exist_ok=True)
            with open(out_path, "wb") as fh:
                fh.write(data)
            print(f"  Wrote {out_path} ({len(data):,} bytes)\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
