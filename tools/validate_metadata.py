"""Lint CloudBolt content metadata.

Checks every <ID>_metadata.json under the content directories:
  - parses as JSON and its folder name matches its "id" (when present)
  - every "<dir>/<ID>" cross-reference points at a folder that has metadata
  - a plugin's script_filename exists
  - no default value carries a GUID, a real Azure subscription path, or an
    instance-specific hostname (defaults must use FILL-ME or <placeholder>)
  - a package manifest (packages/PKG-*) has the eight keys the Content Package
    Manager exporter writes, in its format, a unique name, and only distinct,
    resolvable, top-level members (never forms/ or form_functions/); a member
    that another member's export already carries is a warning
  - a standalone parameter (parameters/CF-*) has a name, a known type, no
    secret option or default values, generated-options refs to
    orchestration_actions/, and belongs to a package (else it never syncs)
  - a Runbooks workflow (runbook_workflows/SWF-*) has a name and steps, lists
    every step's plugin under dependencies.hooks as plugins/OHK-*, and
    belongs to a package (else it never syncs)

Exit code 1 with a list of problems, 0 when clean (warnings do not fail).
No dependencies.
"""
import glob
import json
import os
import re
import sys

CONTENT_DIRS = (
    "blueprints", "plugins", "resource_actions", "server_actions",
    "orchestration_actions", "flowcontrol_actions", "recurring_jobs",
    "cit_tests", "webhooks", "mcp_tool_actions", "shared_modules", "extensions", "forms",
    "form_functions", "parameters", "runbook_workflows",
)
REF_RE = re.compile(r"^(%s)/([A-Z]+-[A-Za-z0-9]+)$" % "|".join(CONTENT_DIRS))
# The nil GUID is an accepted placeholder.
GUID_RE = re.compile(r"\b(?!0{8}-0{4}-0{4}-0{4}-0{12}\b)[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
FORBIDDEN_RE = re.compile(r"(?i)(cblabsales|\bmb-dev\b|\bse-demo\b|/subscriptions/(?!0{8}-)[0-9a-f]{8}-)")
DEFAULT_KEYS = {"value", "default", "default_value", "defaultValue"}
PLACEHOLDER_RE = re.compile(r"FILL-ME|<[a-z][a-z-]*>")

# Content package manifests; mirrors extensions/XUI-iq65xgvr/content_packages/store.py.
PACKAGE_FIELDS = ("contents", "description", "id", "last_updated", "metadata_version",
                  "minimum_version_required", "name", "version")
PKG_ID_RE = re.compile(r"^PKG-[a-z0-9]{8,}$")
PKG_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MEMBER_RE = re.compile(r"^(?P<dir>[a-z_]+)/(?P<prefix>[A-Z]{2,4})-[a-z0-9]{8,}$")
MEMBER_PREFIXES = {
    "blueprints": "BP", "resource_actions": "RSA", "server_actions": "SVA",
    "orchestration_actions": "HPA", "flowcontrol_actions": "FCA", "recurring_jobs": "RJB",
    "cit_tests": "CIT", "webhooks": "IWH", "mcp_tool_actions": "MTA", "shared_modules": "SHM",
    "plugins": "OHK", "extensions": "XUI", "parameters": "CF", "runbook_workflows": "SWF",
}
TRANSITIVE_DIRS = ("forms", "form_functions")

# Standalone parameters; mirrors extensions/XUI-iq65xgvr/content_packages/parameters.py.
CF_ID_RE = re.compile(r"^CF-[a-z0-9]{8,}$")
# infrastructure.models.CustomField.ATTR_TYPES
PARAMETER_TYPES = {"STR", "INT", "IP", "DT", "DTM", "TXT", "ETXT", "CODE", "BOOL", "DEC", "NET", "PWD",
                   "TUP", "LDAP", "URL", "NSXS", "NSXE", "STOR", "FILE", "AAP"}
SECRET_TYPES = {"PWD", "ETXT"}

# Runbooks workflows; mirrors extensions/XUI-iq65xgvr/content_packages/workflows.py and
# runbook_workflows.export_workflow (shared_modules/SHM-nznzhpp0).
SWF_ID_RE = re.compile(r"^SWF-[a-z0-9]{8,}$")
WORKFLOW_FILTERS = {"os_families", "resource_technologies", "os_builds"}
WORKFLOW_INSTANCE_FILTERS = {"groups", "environments", "configuration_managers"}
# Workflows a blueprint's Run Workflow build item pins travel with it; mirrors
# workflows.blueprint_pins in extensions/XUI-iq65xgvr.
RUNNER_HOOK_REF = "plugins/OHK-69gk5ne9"
PIN_DEFAULT_RE = re.compile(r"^workflow_a\d+$")
PIN_QUESTION_RE = re.compile(r"^plugin-bdi-([a-z0-9]+)\.workflow$", re.IGNORECASE)


def walk(obj, path=""):
    """Yield (path, key, value) for every leaf in a JSON structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}")
            if not isinstance(v, (dict, list)):
                yield path, k, v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")


def check_defaults(problems, where, data):
    for path, key, value in walk(data):
        if not isinstance(value, str):
            continue
        if key in DEFAULT_KEYS and not PLACEHOLDER_RE.search(value):
            if GUID_RE.search(value):
                problems.append(f"{where}: default at {path}.{key} contains a GUID; use a placeholder")
        if FORBIDDEN_RE.search(value):
            problems.append(f"{where}: {path}.{key} contains an instance-specific value")


def dependency_refs(obj):
    """Every "<dir>/<ID>" string under any "dependencies" key."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "dependencies" and isinstance(value, dict):
                for dep in value.values():
                    for item in (dep if isinstance(dep, list) else [dep]):
                        if isinstance(item, str) and REF_RE.match(item):
                            yield item
            else:
                yield from dependency_refs(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from dependency_refs(item)


def pinned_workflows(ref, parsed):
    """runbook_workflows/SWF-* refs blueprint ref pins by ID: a workflow_a<N>
    default on a Run Workflow build item, or a defaultValue on that item's
    plugin-bdi-<item>.workflow question in the blueprint's custom form."""
    data = parsed.get(ref) or {}
    values, items = [], set()
    for item in data.get("deployment_items") or []:
        item = item if isinstance(item, dict) else {}
        hook = (item.get("dependencies") or {}).get("hook")
        if hook != RUNNER_HOOK_REF and (parsed.get(hook) or {}).get("name") != "Run Workflow":
            continue
        items.add(str(item.get("id") or "")[4:].lower())
        values += [str(d.get("value") or "") for d in item.get("parameter_defaults") or []
                   if isinstance(d, dict) and PIN_DEFAULT_RE.match(str(d.get("name") or ""))]
    form = parsed.get((data.get("dependencies") or {}).get("custom_form")) or {}
    if items and isinstance(form.get("json"), str):
        try:
            survey = json.loads(form["json"])
        except ValueError:
            survey = {}
        for element in form_elements(survey):
            m = PIN_QUESTION_RE.match(str(element.get("name") or ""))
            if m and m.group(1).lower() in items:
                values.append(str(element.get("defaultValue") or ""))
    return [f"runbook_workflows/{v.strip()}" for v in dict.fromkeys(values) if SWF_ID_RE.match(v.strip())]


def form_elements(node):
    """Every object with a name in a SurveyJS form definition."""
    if isinstance(node, dict):
        if "name" in node:
            yield node
        for value in node.values():
            yield from form_elements(value)
    elif isinstance(node, list):
        for value in node:
            yield from form_elements(value)


def carried_by(ref, parsed):
    """What exporting ref writes along with it: its dependencies, transitively,
    and the workflows a blueprint pins."""
    seen, todo = set(), [ref]
    while todo:
        current = todo.pop()
        refs = list(dependency_refs(parsed.get(current, {})))
        if current.startswith("blueprints/"):
            refs += pinned_workflows(current, parsed)
        for dep in refs:
            if dep not in seen:
                seen.add(dep)
                todo.append(dep)
    seen.discard(ref)
    return seen


def check_package(problems, warnings, p, folder, data, text, known, parsed):
    if not PKG_ID_RE.match(folder):
        problems.append(f"{p}: package folder {folder} is not PKG- plus 8 or more [a-z0-9]")
    if not data.get("id"):
        problems.append(f"{p}: missing id")
    missing = [k for k in PACKAGE_FIELDS if k not in data]
    if missing:
        problems.append(f"{p}: missing package field(s) {', '.join(missing)}")
    unknown = sorted(set(data) - set(PACKAGE_FIELDS))
    if unknown:
        problems.append(f"{p}: unknown package field(s) {', '.join(unknown)}; "
                        "the extension ignores them and the next export drops them")
    name = data.get("name")
    if "name" in data and (not isinstance(name, str) or not name.strip() or len(name) > 255):
        problems.append(f"{p}: name must be non-empty text of at most 255 characters")
    description = data.get("description")
    if "description" in data and (not isinstance(description, str) or len(description) > 1000):
        problems.append(f"{p}: description must be text of at most 1000 characters")
    if "version" in data and not PKG_VERSION_RE.match(str(data["version"])):
        problems.append(f"{p}: version {data['version']!r} is not like 1.0.0")
    if "last_updated" in data and not DATE_RE.match(str(data["last_updated"])):
        problems.append(f"{p}: last_updated must be YYYY-MM-DD")
    for key in ("metadata_version", "minimum_version_required"):
        if key in data and not isinstance(data[key], str):
            problems.append(f"{p}: {key} must be a string")

    contents = data.get("contents")
    if "contents" in data and not isinstance(contents, list):
        problems.append(f'{p}: contents must be a list of "<top_level_dir>/<GLOBAL_ID>" refs')
        contents = []
    members = []
    for ref in contents or []:
        m = MEMBER_RE.match(ref) if isinstance(ref, str) else None
        if not m:
            problems.append(f'{p}: member {ref!r} is not "<top_level_dir>/<GLOBAL_ID>"')
        elif m.group("dir") in TRANSITIVE_DIRS:
            problems.append(f"{p}: member {ref} only syncs with its parent; list the parent instead")
        elif m.group("dir") not in MEMBER_PREFIXES:
            problems.append(f"{p}: member {ref}: {m.group('dir')}/ is not a type a package can hold")
        elif m.group("prefix") != MEMBER_PREFIXES[m.group("dir")]:
            problems.append(f"{p}: member {ref}: {m.group('dir')}/ holds {MEMBER_PREFIXES[m.group('dir')]}- IDs")
        elif ref in members:
            problems.append(f"{p}: member {ref} is listed twice")
        elif ref not in known:
            problems.append(f"{p}: member {ref} does not resolve")
        else:
            members.append(ref)
    for ref in members:
        parents = [other for other in members if other != ref and ref in carried_by(other, parsed)]
        if parents:
            warnings.append(f"{p}: member {ref} already travels with {', '.join(parents)}; drop it")

    if not missing and not unknown and text != json.dumps(data, indent=4, sort_keys=True) + "\n":
        problems.append(f"{p}: not in the exporter's format (json.dumps(indent=4, sort_keys=True) "
                        "plus a newline), so the next export would rewrite it")


def check_parameter(problems, warnings, p, folder, data, text):
    if not CF_ID_RE.match(folder):
        problems.append(f"{p}: parameter folder {folder} is not CF- plus 8 or more [a-z0-9]")
    if not data.get("id"):
        problems.append(f"{p}: missing id")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        problems.append(f"{p}: missing name; sync matches the parameter by name")
    if len(data.get("label") or "") > 50:
        problems.append(f"{p}: label is longer than 50 characters")
    kind = data.get("type")
    if kind not in PARAMETER_TYPES:
        problems.append(f"{p}: type {kind!r} is not a CustomField type code")
    if kind in SECRET_TYPES and (data.get("global_options") or data.get("global_defaults")):
        problems.append(f"{p}: a {kind} parameter must not carry global option or default values")
    for i, hook in enumerate(data.get("gen_options_hooks") or []):
        ref = ((hook or {}).get("dependencies") or {}).get("orchestration_hook")
        if not isinstance(ref, str) or not ref.startswith("orchestration_actions/HPA-"):
            problems.append(f"{p}: gen_options_hooks[{i}] needs dependencies.orchestration_hook "
                            "orchestration_actions/HPA-*")
    if text != json.dumps(data, indent=4, sort_keys=True) + "\n":
        warnings.append(f"{p}: not in the exporter's format (json.dumps(indent=4, sort_keys=True) "
                        "plus a newline), so the next export rewrites it")


def check_workflow(problems, warnings, p, folder, data, text):
    if not SWF_ID_RE.match(folder):
        problems.append(f"{p}: workflow folder {folder} is not SWF- plus 8 or more [a-z0-9]")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        problems.append(f"{p}: missing name")
    filters = data.get("filters")
    if not isinstance(filters, dict) or set(filters) - WORKFLOW_FILTERS:
        problems.append(f"{p}: filters must be an object of {', '.join(sorted(WORKFLOW_FILTERS))} name lists")
    instance = data.get("instance_filters")
    if not isinstance(instance, list) or set(instance) - WORKFLOW_INSTANCE_FILTERS:
        problems.append(f"{p}: instance_filters must list only {', '.join(sorted(WORKFLOW_INSTANCE_FILTERS))}")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append(f"{p}: a workflow needs at least one step")
        steps = []
    hooks = (data.get("dependencies") or {}).get("hooks")
    hooks = hooks if isinstance(hooks, list) else []
    for i, step in enumerate(steps):
        step = step if isinstance(step, dict) else {}
        action = step.get("action")
        if not isinstance(action, str) or not action.startswith("OHK-"):
            problems.append(f"{p}: steps[{i}] needs an action OHK-*")
        elif f"plugins/{action}" not in hooks:
            problems.append(f"{p}: steps[{i}] runs {action}, which dependencies.hooks does not list "
                            "as plugins/" + action)
        if not step.get("action_name"):
            problems.append(f"{p}: steps[{i}] needs action_name; sync finds a renumbered plugin by it")
    if text != json.dumps(data, indent=4, sort_keys=True) + "\n":
        warnings.append(f"{p}: not in the exporter's format (json.dumps(indent=4, sort_keys=True) "
                        "plus a newline), so the next export rewrites it")


def main(root="."):
    os.chdir(root)
    problems = []
    warnings = []
    metadata_files = sorted(glob.glob("*/*/*_metadata.json"))
    if not metadata_files:
        print("no metadata files found")
        return 1
    known = {}
    for p in metadata_files:
        top, folder, _ = p.replace("\\", "/").split("/")
        known[f"{top}/{folder}"] = p

    parsed = {}
    packages = []
    parameters = []  # (path, ref) of the parameters and workflows only a package syncs
    for p in metadata_files:
        p = p.replace("\\", "/")
        top, folder, fname = p.split("/")
        try:
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
            data = json.loads(text)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{p}: invalid JSON ({e})")
            continue
        parsed[f"{top}/{folder}"] = data
        if fname != f"{folder}_metadata.json":
            problems.append(f"{p}: file name does not match folder {folder}")
        if data.get("id") and data["id"] != folder:
            problems.append(f"{p}: id {data['id']} does not match folder {folder}")
        check_defaults(problems, p, data)
        if top == "packages":
            # Members are checked once every unit is parsed (see below).
            packages.append((p, folder, data, text))
            continue
        if top == "parameters":
            check_parameter(problems, warnings, p, folder, data, text)
            parameters.append((p, f"{top}/{folder}"))
        if top == "runbook_workflows":
            check_workflow(problems, warnings, p, folder, data, text)
            parameters.append((p, f"{top}/{folder}"))

        script = data.get("script_filename")
        if script and not os.path.exists(f"{top}/{folder}/{script}"):
            problems.append(f"{p}: script_filename {script} not found")

        # CloudBolt column limits (infrastructure.models.CustomField). A longer value fails
        # the whole import with a bare "value too long for type character varying(N)".
        for ai in data.get("action_inputs") or []:
            if not isinstance(ai, dict):
                continue
            name = ai.get("name", "?")
            if len(ai.get("label") or "") > 50:
                problems.append(f"{p}: action input {name} label is longer than 50 characters")
            if len(ai.get("placeholder") or "") > 255:
                problems.append(f"{p}: action input {name} placeholder is longer than 255 characters")

        for _, _, value in walk(data):
            if isinstance(value, str):
                m = REF_RE.match(value)
                if m and value not in known:
                    problems.append(f"{p}: reference {value} does not resolve")

        # Custom forms keep their definition as a JSON string; lint its defaults too.
        if top == "forms" and isinstance(data.get("json"), str):
            try:
                check_defaults(problems, p, json.loads(data["json"]))
            except Exception as e:  # noqa: BLE001
                problems.append(f"{p}: form json field is not valid JSON ({e})")

    names = {}
    for p, folder, data, text in packages:
        check_package(problems, warnings, p, folder, data, text, known, parsed)
        if isinstance(data.get("name"), str):
            names.setdefault(data["name"].strip().lower(), []).append(folder)
    for folders in names.values():
        if len(folders) > 1:
            # The extension keys packages by id but refuses a second package with the same name.
            problems.append(f"packages: {', '.join(folders)} share a name; an appliance can import only one")
    members = {ref for _, _, data, _ in packages for ref in data.get("contents") or [] if isinstance(ref, str)}
    for p, ref in parameters:
        if ref not in members:
            warnings.append(f"{p}: no package lists {ref}, so it never syncs; add it to the package that needs it")

    for msg in problems:
        print("PROBLEM:", msg)
    for msg in warnings:
        print("WARNING:", msg)
    summary = f"checked {len(metadata_files)} metadata files, {len(problems)} problem(s)"
    if warnings:
        summary += f", {len(warnings)} warning(s)"
    print(summary)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
