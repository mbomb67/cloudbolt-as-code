"""Lint CloudBolt content metadata.

Checks every <ID>_metadata.json under the content directories:
  - parses as JSON and its folder name matches its "id" (when present)
  - every "<dir>/<ID>" cross-reference points at a folder that has metadata
  - a plugin's script_filename exists
  - no default value carries a GUID, a real Azure subscription path, or an
    instance-specific hostname (defaults must use FILL-ME or <placeholder>)

Exit code 1 with a list of problems, 0 when clean. No dependencies.
"""
import glob
import json
import os
import re
import sys

CONTENT_DIRS = (
    "blueprints", "plugins", "resource_actions", "server_actions",
    "orchestration_actions", "flowcontrol_actions", "recurring_jobs",
    "cit_tests", "webhooks", "shared_modules", "extensions", "forms",
    "form_functions",
)
REF_RE = re.compile(r"^(%s)/([A-Z]+-[A-Za-z0-9]+)$" % "|".join(CONTENT_DIRS))
# The nil GUID is an accepted placeholder.
GUID_RE = re.compile(r"\b(?!0{8}-0{4}-0{4}-0{4}-0{12}\b)[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
FORBIDDEN_RE = re.compile(r"(?i)(cblabsales|\bmb-dev\b|\bse-demo\b|/subscriptions/(?!0{8}-)[0-9a-f]{8}-)")
DEFAULT_KEYS = {"value", "default", "default_value", "defaultValue"}
PLACEHOLDER_RE = re.compile(r"FILL-ME|<[a-z][a-z-]*>")


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


def main(root="."):
    os.chdir(root)
    problems = []
    metadata_files = sorted(glob.glob("*/*/*_metadata.json"))
    if not metadata_files:
        print("no metadata files found")
        return 1
    known = {}
    for p in metadata_files:
        top, folder, _ = p.replace("\\", "/").split("/")
        known[f"{top}/{folder}"] = p

    for p in metadata_files:
        p = p.replace("\\", "/")
        top, folder, fname = p.split("/")
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            problems.append(f"{p}: invalid JSON ({e})")
            continue
        if fname != f"{folder}_metadata.json":
            problems.append(f"{p}: file name does not match folder {folder}")
        if data.get("id") and data["id"] != folder:
            problems.append(f"{p}: id {data['id']} does not match folder {folder}")

        script = data.get("script_filename")
        if script and not os.path.exists(f"{top}/{folder}/{script}"):
            problems.append(f"{p}: script_filename {script} not found")

        for _, _, value in walk(data):
            if isinstance(value, str):
                m = REF_RE.match(value)
                if m and value not in known:
                    problems.append(f"{p}: reference {value} does not resolve")

        check_defaults(problems, p, data)
        # Custom forms keep their definition as a JSON string; lint its defaults too.
        if top == "forms" and isinstance(data.get("json"), str):
            try:
                check_defaults(problems, p, json.loads(data["json"]))
            except Exception as e:  # noqa: BLE001
                problems.append(f"{p}: form json field is not valid JSON ({e})")

    for msg in problems:
        print("PROBLEM:", msg)
    print(f"checked {len(metadata_files)} metadata files, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
