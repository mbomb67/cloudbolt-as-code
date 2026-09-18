"""
Bicep deployment engine for CloudBolt CMP.

Layering (mirrors the HCP Terraform integration's client/engine split):

  * Pure logic — binary bootstrap, tarball extraction, compilation, ARM-JSON
    parameter-schema extraction, parameter assembly/validation, and the
    BicepArmClient REST client — carries NO CloudBolt job imports and never
    touches a Resource.
  * run_with_approval(job, ...) — the ONLY surface that touches the job
    (job.pause / set_progress). It never mutates the Resource; custom-field
    mutation stays in the plugins.

Scopes: the engine deploys resource-group-scoped templates (into a caller-
supplied RG) and subscription-scoped templates (no RG; e.g. templates that
create resource groups). The scope is detected from the compiled ARM $schema
(detect_target_scope), never guessed from inputs.

Vendor APIs (Azure ARM deployment stacks, deployment what-if, the Bicep CLI,
the Azure login token endpoint) are cited at each call site per AGENTS.md
cardinal rule 5. Request/response shapes were grounded against current docs at
author time; re-verify on major Azure API changes. Secrets — the minted ARM
access token and any @secure() parameter value — never appear in a URL, log
line, exception message, or job output (cardinal rule 3).

This repo has no prior art for subprocess/binary/tarball work in plugins or
shared_modules; the closest precedent is in extensions/ Django views
(settings.PROSERV_DIR writes in XUI-ycumvq8k, NamedTemporaryFile + shell-out in
XUI-i4kzuy3y). Those run outside the jobengine, so whether a jobengine worker
may exec a downloaded binary from the cache dir (no noexec mount / SELinux
denial) MUST be verified on a representative appliance before relying on this
module (see docs/bicep-deployment-setup.md).
"""
import ast
import base64
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

import requests
from django.conf import settings

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# ---------------------------------------------------------------------------
# Config block — reviewable, versioned (this repo is config-as-code).
# Operators adjust these per docs/bicep-deployment-setup.md.
# ---------------------------------------------------------------------------

# Pinned Bicep CLI. The standalone binary is self-contained (no .NET runtime).
# IMPORTANT: BICEP_SHA256 must be independently verified against Microsoft's
# published release checksum — NOT taken on faith from the same change that
# sets BICEP_DOWNLOAD_URL_TEMPLATE (a coordinated url+checksum edit is the one
# supply-chain hole the checksum cannot close on its own). A PR touching both
# is security-sensitive and needs explicit sign-off.
BICEP_VERSION = "0.44.1"  # operator-pinned; bump re-validates via versioned path
BICEP_SHA256 = "e17dc9a9888184886bb0c0051a3230b83b19f342749999f707bc571c3dfd2f45"
# Linux x64 appliance default; musl/osx/win variants per the install docs.
# https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/install
BICEP_DOWNLOAD_URL_TEMPLATE = (
    "https://github.com/Azure/bicep/releases/download/v{version}/bicep-linux-x64"
)
# Cache root under CloudBolt's sanctioned writable dir (the location
# extensions/XUI-ycumvq8k/.../gen_rh_status.py writes to). The binary lands at
# <cache>/bicep/<version>/bicep so a version bump never races an in-use binary.
BICEP_CACHE_ROOT = os.path.join(settings.PROSERV_DIR, "bicep")

# Bounded sizes / timeouts.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024  # reject oversized compressed repo archives
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024  # reject decompression bombs
# Sparse fetch (fetch_template_checkout): only the template's own directory
# subtree — plus any bicepconfig.json in its ancestor directories — is pulled
# through the GitHub Contents API, so a template hosted in a very large public
# repo (Azure/azure-quickstart-templates is >250 MB compressed) costs kilobytes
# per order. The full tarball remains the fallback when the folder references
# files outside itself ('../' module/import/load paths), sits at the repo root,
# or the Contents API cannot serve it. These caps bound the sparse path.
MAX_SPARSE_FILES = 500
MAX_SPARSE_FILE_BYTES = 25 * 1024 * 1024
BICEP_CONFIG_FILENAME = "bicepconfig.json"
MAX_BINARY_BYTES = 256 * 1024 * 1024  # cap the streamed Bicep binary download
COMPILE_TIMEOUT_S = 120
DOWNLOAD_TIMEOUT_S = 120
HTTP_TIMEOUT_S = 60
STACK_POLL_TIMEOUT_S = 60 * 30          # bounded apply/poll window
STACK_POLL_INTERVAL_S = 10
WHATIF_POLL_TIMEOUT_S = 60 * 10
WHATIF_POLL_INTERVAL_S = 5

# Azure endpoints.
ARM_BASE = "https://management.azure.com"
AZURE_LOGIN_BASE = "https://login.microsoftonline.com"
ARM_SCOPE = "https://management.azure.com/.default"
# api-versions: deployment stacks GA is 2024-03-01 (the create-or-update REST
# reference still defaults its moniker to 2022-08-01-preview — confirm the GA
# version is enabled in the target tenant). what-if is on the deployments API.
DEPLOYMENT_STACKS_API_VERSION = "2024-03-01"
WHATIF_API_VERSION = "2021-04-01"

# Template scopes the engine deploys. Detected from the compiled ARM JSON's
# $schema (see detect_target_scope); a resource-group-scoped template needs a
# target resource group, a subscription-scoped one (e.g. a template that CREATES
# resource groups) must not have one. Management-group/tenant scopes are
# recognised but rejected (out of scope for this engine).
SCOPE_RESOURCE_GROUP = "resourceGroup"
SCOPE_SUBSCRIPTION = "subscription"
SCOPE_MANAGEMENT_GROUP = "managementGroup"
SCOPE_TENANT = "tenant"
SUPPORTED_SCOPES = (SCOPE_RESOURCE_GROUP, SCOPE_SUBSCRIPTION)

# Subscription-scoped stacks and what-ifs must carry a `location` (where ARM
# stores the deployment metadata; RG-scoped stacks inherit the RG's). The
# plugins prefer the value of a template parameter named in
# STACK_LOCATION_PARAM_CANDIDATES (first match wins) so the metadata lands
# beside the resources; otherwise DEFAULT_STACK_LOCATION is used.
DEFAULT_STACK_LOCATION = "eastus"
STACK_LOCATION_PARAM_CANDIDATES = ["rgLocation", "resourceGroupLocation", "location"]

# Deny-settings default for the POC: none. When set to denyDelete /
# denyWriteAndDelete, create_or_update_stack asserts the engine SP is excluded
# (see _assert_self_excluded) so the engine can still manage its own stack.
DENY_SETTINGS_MODE = "none"

# Output capture: only outputs named here become custom fields. None = all
# non-secure outputs (secure outputs come back null from ARM regardless).
OUTPUT_ALLOWLIST = None
# The first matching output name (case-insensitive) sets resource.name.
RESOURCE_NAME_OUTPUT_CANDIDATES = ["resourceName", "resource_name", "name", "vmName"]
# Fallback when the template exposes no such output: the first matching
# (non-secure, string) PARAMETER name sets resource.name instead.
RESOURCE_NAME_PARAM_CANDIDATES = ["rgName", "resourceGroupName", "resourceName", "name"]

# Namespacing for resource custom fields the engine reads/writes.
VAR_CF_PREFIX = "bicep_var_"
OUTPUT_CF_PREFIX = "bicep_out_"

# Acronyms/initialisms uppercased in humanized labels (lowercase keys).
_LABEL_ACRONYMS = {
    "id", "url", "uri", "ip", "vm", "vmss", "sku", "ssl", "tls", "dns", "vnet",
    "nic", "nsg", "rg", "os", "cpu", "ram", "sas", "api", "arm", "json", "yaml",
    "html", "http", "https", "tcp", "udp", "db", "fqdn", "cidr", "acr", "aks",
    "lb", "ssh", "az", "ad", "aad", "mfa", "rbac", "sku",
}


def humanize_label(name):
    """Turn a Bicep parameter/output identifier into a human-readable label.

    Strips no prefix itself (callers pass the BARE name, e.g. "storageAccountId"
    — never "bicep_out_..."): splits camelCase and snake_case/kebab into words,
    title-cases them, and uppercases known acronyms. So "storageAccountId" ->
    "Storage Account ID", "resource_group_name" -> "Resource Group Name",
    "vmSize" -> "VM Size". The bicep_var_/bicep_out_ prefix stays in the custom
    field NAME; only the label is humanized.
    """
    s = name.replace("_", " ").replace("-", " ")
    # camelCase / PascalCase boundaries: aB -> a B, and ABc -> A Bc (acronym run
    # followed by a capitalized word).
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    words = []
    for w in s.split():
        if w.lower() in _LABEL_ACRONYMS:
            words.append(w.upper())
        else:
            words.append(w[:1].upper() + w[1:])
    return " ".join(words) or name


def coerce_params_payload(value):
    """Normalize a parsed parameters payload to a flat dict.

    A SurveyJS Dynamic Panel always submits a LIST of parameter-sets (it can
    hold 0..N panels), but this engine deploys a single set. A one-element list
    is unwrapped to its dict; a multi-element list is merged (later entries win)
    defensively; a dict passes through unchanged.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        merged = {}
        for item in value:
            if isinstance(item, dict):
                merged.update(item)
        return merged
    if not value:
        return {}
    raise BicepEngineError(
        "Parameters payload must be a JSON object or a list of objects.")


def parse_params_payload(raw):
    """Parse a rendered parameters payload (str, dict, or list) into a flat dict.

    CloudBolt renders a complex action-input value via str() — a single-quoted
    Python repr — so this tries ast.literal_eval first (the AGENTS.md cardinal-
    rule-3 pattern for list/dict template vars) and falls back to json.loads for
    a JSON string carrying true/false/null. The result is then normalized: a
    SurveyJS Dynamic Panel's single-item list[dict] is stripped to the dict.
    """
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return coerce_params_payload(raw)
    text = str(raw).strip()
    if not text:
        return {}
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            raise BicepEngineError(
                "The parameters input is not a valid object literal or JSON.")
    return coerce_params_payload(value)


# ARM provisioningState buckets (DeploymentStackProvisioningState enum).
STACK_TERMINAL_SUCCESS = {"succeeded"}
STACK_TERMINAL_FAILURE = {"failed", "canceled"}
STACK_IN_FLIGHT = {
    "creating", "validating", "waiting", "deploying", "canceling",
    "updatingDenyAssignments", "deletingResources", "deleting",
}


# ===========================================================================
# U2 — Toolchain: binary bootstrap, tarball extraction, compile, schema
# ===========================================================================


class BicepEngineError(Exception):
    """Operator-legible engine error. Never carries a token or secret value."""


def _versioned_binary_path(version=BICEP_VERSION):
    return os.path.join(BICEP_CACHE_ROOT, version, "bicep")


def _sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fd:
        for chunk in iter(lambda: fd.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_bicep_binary(version=BICEP_VERSION, sha256=BICEP_SHA256):
    """
    Return the path to the verified, executable Bicep binary, bootstrapping it
    on first use. Concurrency-safe: download to a unique temp path, verify the
    checksum THERE, chmod, then atomically rename into the version-qualified
    final path. If another jobengine worker produced the verified binary first
    (the file appears between our check and our rename), that is success, not an
    error. This is the load-bearing detail — this repo has no inter-job locking
    precedent, so correctness rests on the atomic rename, not a lock.
    """
    final_path = _versioned_binary_path(version)

    # Fast path: already cached and intact.
    if os.path.exists(final_path):
        if _sha256_of_file(final_path) == sha256:
            return final_path
        # Corrupt/partial cached copy (e.g. an interrupted earlier rename on a
        # filesystem where rename was not atomic) — replace it.
        logger.warning("Cached Bicep binary failed checksum; re-bootstrapping.")

    version_dir = os.path.join(BICEP_CACHE_ROOT, version)
    try:
        os.makedirs(version_dir, exist_ok=True)
    except OSError as e:
        raise BicepEngineError(
            f"Cannot create Bicep cache dir {version_dir}: {e}. Confirm "
            f"settings.PROSERV_DIR is writable by the CloudBolt service account."
        )

    url = BICEP_DOWNLOAD_URL_TEMPLATE.format(version=version)
    set_progress(f"Bootstrapping Bicep {version}...")
    fd, tmp_path = tempfile.mkstemp(prefix=".bicep-dl-", dir=version_dir)
    os.close(fd)
    try:
        try:
            resp = requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_S)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise BicepEngineError(
                f"Bicep binary download failed from {url}: {e}. Check appliance "
                f"egress or set a mirror URL in the engine config block."
            )
        downloaded = 0
        with open(tmp_path, "wb") as out:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                downloaded += len(chunk)
                if downloaded > MAX_BINARY_BYTES:
                    raise BicepEngineError(
                        f"Bicep binary download exceeded {MAX_BINARY_BYTES} bytes; "
                        f"aborting (possible misconfigured or hostile mirror)."
                    )
                out.write(chunk)

        actual = _sha256_of_file(tmp_path)
        if actual != sha256:
            raise BicepEngineError(
                f"Bicep {version} checksum mismatch (expected {sha256[:12]}…, "
                f"got {actual[:12]}…). Refusing to execute an unverified binary. "
                f"Verify BICEP_SHA256 against Microsoft's published release hash."
            )
        os.chmod(tmp_path, 0o755)
        try:
            os.replace(tmp_path, final_path)  # atomic on the same filesystem
            tmp_path = None
        except OSError:
            # Lost the race — another worker already produced the binary.
            if os.path.exists(final_path) and \
                    _sha256_of_file(final_path) == sha256:
                return final_path
            raise
        return final_path
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _is_within(base, target):
    base = os.path.realpath(base)
    target = os.path.realpath(target)
    return target == base or target.startswith(base + os.sep)


# Any quoted path that climbs out of the template's directory. Matches module /
# import / using / loadTextContent-style references such as '../shared/x.bicep'.
_PARENT_REF_RE = re.compile(r"""['"]\.\.[/\\]""")


def _needs_full_checkout(dir_path):
    """True when a .bicep/.bicepparam file under dir_path references a parent
    path — the sparse folder fetch cannot satisfy that, so the caller falls
    back to the full archive."""
    for root, _, files in os.walk(dir_path):
        for fname in files:
            if not fname.endswith((".bicep", ".bicepparam")):
                continue
            try:
                with open(os.path.join(root, fname), "r", encoding="utf-8",
                          errors="ignore") as fd:
                    if _PARENT_REF_RE.search(fd.read()):
                        return True
            except OSError:
                continue
    return False


def fetch_template_directory(gh, repo, ref, template_path, dest_dir):
    """
    Sparse fetch: download ONLY the template's directory subtree (recursively)
    plus any bicepconfig.json found in its ancestor directories, recreating the
    repo-relative layout under <dest_dir>/checkout so compile_template() can
    join the same template_path. Returns the checkout root.

    `gh` is a GitHubConnection (shared_modules.github) providing
    get_repo_contents(repo, path, ref) and get_repo_file(repo, path, ref,
    max_bytes). Raises BicepEngineError on any limit/shape problem so the
    caller can fall back to the tarball; GitHub HTTP failures propagate as the
    client's own exceptions (also caught by the caller).
    """
    template_path = template_path.strip("/").replace("\\", "/")
    tdir = posixpath.dirname(template_path)
    if not tdir:
        raise BicepEngineError(
            "Template sits at the repository root; a sparse fetch would pull "
            "the whole repository.")
    checkout = os.path.join(dest_dir, "checkout")
    os.makedirs(checkout, exist_ok=True)
    counts = {"files": 0, "bytes": 0}

    def _write(rel_path, data):
        target = os.path.join(checkout, *rel_path.split("/"))
        if not _is_within(checkout, target):
            raise BicepEngineError(
                f"Repository path escapes the checkout root ({rel_path}).")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fd:
            fd.write(data)

    def _walk(path):
        listing = gh.get_repo_contents(repo, path, ref)
        if listing is None:
            raise BicepEngineError(
                f"'{path}' was not found in {repo}@{ref or 'default-branch'}.")
        if isinstance(listing, dict):
            listing = [listing]
        for entry in listing:
            etype, epath = entry.get("type"), entry.get("path") or ""
            if etype == "dir":
                _walk(epath)
            elif etype == "file":
                size = int(entry.get("size") or 0)
                if size > MAX_SPARSE_FILE_BYTES:
                    raise BicepEngineError(
                        f"'{epath}' is {size} bytes, over the sparse per-file cap.")
                counts["files"] += 1
                counts["bytes"] += size
                if counts["files"] > MAX_SPARSE_FILES:
                    raise BicepEngineError(
                        f"Template directory holds more than {MAX_SPARSE_FILES} "
                        f"files; too large for a sparse fetch.")
                if counts["bytes"] > MAX_UNCOMPRESSED_BYTES:
                    raise BicepEngineError(
                        "Template directory exceeds the checkout size cap.")
                _write(epath, gh.get_repo_file(repo, epath, ref,
                                               max_bytes=MAX_SPARSE_FILE_BYTES))
            else:
                # symlink / submodule entries are skipped — the tarball path
                # refuses non-regular members for the same reason.
                logger.debug(f"Skipping {etype} entry '{epath}' in sparse fetch.")

    set_progress(f"Fetching {tdir}/ from {repo}@{ref or 'default-branch'} "
                 f"(sparse)...")
    _walk(tdir)
    if not os.path.isfile(os.path.join(checkout, *template_path.split("/"))):
        raise BicepEngineError(
            f"Template '{template_path}' was not among the fetched files.")

    # bicepconfig.json is discovered by Bicep walking UP from the template, so
    # pull any that exist in the ancestor directories (root ... parent).
    parts = tdir.split("/")
    for depth in range(len(parts)):
        anc = "/".join(parts[:depth])
        cfg = f"{anc}/{BICEP_CONFIG_FILENAME}" if anc else BICEP_CONFIG_FILENAME
        meta = gh.get_repo_contents(repo, cfg, ref)
        if isinstance(meta, dict) and meta.get("type") == "file":
            _write(cfg, gh.get_repo_file(repo, cfg, ref,
                                         max_bytes=MAX_SPARSE_FILE_BYTES))
    return checkout


def fetch_template_checkout(gh, repo, ref, template_path, workdir):
    """
    Obtain a compilable checkout for `template_path`: the sparse directory
    fetch first, the full repository tarball (safe_extract_tarball) as the
    fallback. Returns the checkout root to pass to compile_template().

    Fallback triggers: the template is at the repo root, its folder references
    files outside itself ('../'), the folder is over the sparse caps, or the
    Contents API call fails. A repo/ref/auth problem therefore surfaces through
    the tarball path's own error message.
    """
    sparse_dir = os.path.join(workdir, "sparse")
    try:
        checkout = fetch_template_directory(gh, repo, ref, template_path, sparse_dir)
        tdir = posixpath.dirname(template_path.strip("/").replace("\\", "/"))
        if not _needs_full_checkout(os.path.join(checkout, *tdir.split("/"))):
            return checkout
        set_progress("Template references files outside its own folder; "
                     "fetching the full repository archive instead...")
    except BicepEngineError as e:
        set_progress(f"Sparse fetch not possible ({e}); fetching the full "
                     f"repository archive...")
    except Exception as e:  # noqa: BLE001 — GitHub client raises plain Exception
        logger.debug(f"Sparse fetch failed: {e}")
        set_progress("Sparse fetch failed; fetching the full repository archive...")
    shutil.rmtree(sparse_dir, ignore_errors=True)
    archive = gh.get_repo_archive(repo, ref)
    return safe_extract_tarball(archive, workdir)


def safe_extract_tarball(archive_bytes, dest_dir):
    """
    Extract a GitHub repo tarball into dest_dir, rejecting path-traversal and
    non-regular members (CVE-2007-4559 class). GitHub nests everything under a
    single <owner>-<repo>-<sha>/ top dir; we validate every member's resolved
    path stays within dest_dir BEFORE extracting, reject symlinks/hardlinks/
    devices, then return the path of that single top dir.
    """
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise BicepEngineError(
            f"Repo archive exceeds {MAX_ARCHIVE_BYTES} bytes; refusing to "
            f"extract. The sparse (per-directory) fetch was not possible for "
            f"this template — keep the template and everything it references "
            f"inside one directory (no '../' paths, not at the repo root) so "
            f"only that directory is downloaded, or raise MAX_ARCHIVE_BYTES."
        )
    tmp_tar = os.path.join(dest_dir, "_archive.tar.gz")
    with open(tmp_tar, "wb") as fd:
        fd.write(archive_bytes)
    top_dirs = set()
    total_size = 0
    with tarfile.open(tmp_tar, "r:gz") as tar:
        for member in tar.getmembers():
            if member.issym() or member.islnk() or member.isdev():
                raise BicepEngineError(
                    f"Archive contains a non-regular member ({member.name}); "
                    f"refusing to extract for safety."
                )
            total_size += getattr(member, "size", 0)
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise BicepEngineError(
                    f"Archive expands beyond {MAX_UNCOMPRESSED_BYTES} bytes; "
                    f"refusing to extract (possible decompression bomb)."
                )
            member_path = os.path.join(dest_dir, member.name)
            if not _is_within(dest_dir, member_path):
                raise BicepEngineError(
                    f"Archive member escapes extraction root ({member.name}); "
                    f"refusing to extract."
                )
            top = member.name.split("/", 1)[0]
            if top:
                top_dirs.add(top)
        tar.extractall(dest_dir)  # safe: every member validated above
    os.remove(tmp_tar)
    if len(top_dirs) != 1:
        # Unexpected for a GitHub archive; fall back to dest_dir itself.
        return dest_dir
    return os.path.join(dest_dir, top_dirs.pop())


def compile_template(checkout_root, template_path, version=BICEP_VERSION):
    """
    Compile <checkout_root>/<template_path> to ARM JSON and return the parsed
    dict. If template_path is already ARM JSON (.json), it is parsed directly
    (pre-compiled path is out of POC scope for .bicep-only inputs, but parsing
    a committed .json costs nothing). Compile errors are classified author
    (template bug) vs operator (module restore / egress) so the right person is
    sent to debug.

    Bicep CLI: `bicep build <file> --stdout`
    https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/bicep-cli
    """
    full = os.path.join(checkout_root, template_path)
    if not os.path.isfile(full):
        raise BicepEngineError(
            f"Template path '{template_path}' not found in the repo archive."
        )
    if full.endswith(".json"):
        with open(full, "r", encoding="utf-8") as fd:
            return json.load(fd)

    binary = ensure_bicep_binary(version)
    set_progress(f"Compiling {template_path}...")
    try:
        proc = subprocess.run(
            [binary, "build", full, "--stdout"],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT_S,
        )
    except FileNotFoundError:
        raise BicepEngineError(
            "Bicep binary is present but could not be executed. The cache dir "
            "may be on a noexec mount or blocked by SELinux — see the appliance "
            "exec-permission prerequisite in docs/bicep-deployment-setup.md."
        )
    except subprocess.TimeoutExpired:
        raise BicepEngineError(
            f"Bicep compile timed out after {COMPILE_TIMEOUT_S}s."
        )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        # Module-restore / egress failures are the operator's problem; other
        # diagnostics are the template author's. Surface the Bicep diagnostic,
        # never a raw Python traceback.
        if re.search(r"restore|registry|mcr\.microsoft|br:|module", stderr, re.I):
            raise BicepEngineError(
                f"Bicep module restore failed (check appliance egress to the "
                f"module registry): {stderr}"
            )
        raise BicepEngineError(f"Bicep compile error in {template_path}: {stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise BicepEngineError(f"Compiled ARM JSON was not parseable: {e}")


def _resolve_ref(schema_node, definitions):
    """Resolve a single #/definitions/<name> $ref one level (languageVersion 2.0)."""
    ref = schema_node.get("$ref")
    if not ref:
        return schema_node
    name = ref.split("/")[-1]
    if name not in definitions:
        raise BicepEngineError(
            f"Parameter references undefined type '{ref}'; cannot build a "
            f"reliable input schema. Resolve the template's type definitions."
        )
    merged = dict(definitions[name])
    merged.update({k: v for k, v in schema_node.items() if k != "$ref"})
    return merged


def extract_parameter_schema(arm_json):
    """
    Build a normalized parameter schema from compiled ARM JSON. Each entry:
      {name: {type, required, default, has_expression_default, allowed,
              min_value, max_value, min_length, max_length, secure,
              description, complex (object/array/UDT -> JSON free-text input)}}
    Required = no defaultValue key. Expression defaults ('[...]') are flagged and
    must not be treated as pinnable literals (AE3). Untyped object/array and
    user-defined types ($ref/definitions) become complex JSON free-text inputs.
    ARM JSON shape: https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/syntax
    """
    params = arm_json.get("parameters", {}) or {}
    definitions = arm_json.get("definitions", {}) or {}
    schema = {}
    for name, raw in params.items():
        node = _resolve_ref(raw, definitions) if "$ref" in raw else raw
        atype = (node.get("type") or "").lower()
        secure = atype in ("securestring", "secureobject")
        has_default = "defaultValue" in node
        default = node.get("defaultValue")
        is_expr = (
            has_default and isinstance(default, str)
            and default.startswith("[") and default.endswith("]")
        )
        complex_type = atype in ("object", "secureobject", "array") or "$ref" in raw
        schema[name] = {
            "type": atype,
            "required": not has_default,
            "default": default,
            "has_default": has_default,
            "has_expression_default": is_expr,
            "allowed": node.get("allowedValues"),
            "min_value": node.get("minValue"),
            "max_value": node.get("maxValue"),
            "min_length": node.get("minLength"),
            "max_length": node.get("maxLength"),
            "secure": secure,
            "description": (node.get("metadata") or {}).get("description", ""),
            "complex": complex_type,
        }
    return schema


# Most-specific marker first: "deploymenttemplate" is a substring of the others.
# $schema values per
# https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/syntax
#   resource group   .../deploymentTemplate.json#
#   subscription     .../subscriptionDeploymentTemplate.json#
#   management group .../managementGroupDeploymentTemplate.json#
#   tenant           .../tenantDeploymentTemplate.json#
_SCHEMA_SCOPE_MARKERS = (
    ("subscriptiondeploymenttemplate", SCOPE_SUBSCRIPTION),
    ("managementgroupdeploymenttemplate", SCOPE_MANAGEMENT_GROUP),
    ("tenantdeploymenttemplate", SCOPE_TENANT),
    ("deploymenttemplate", SCOPE_RESOURCE_GROUP),
)


def detect_target_scope(arm_json):
    """
    Return the deployment scope a compiled template targets — one of the
    SCOPE_* constants — from its ARM `$schema`. Bicep's `targetScope` compiles
    to exactly this schema URL, so a template that creates resource groups
    (`targetScope = 'subscription'`) is recognised without any template-
    specific knowledge. A missing/unknown $schema is treated as resource-group
    scope (the ARM default).
    """
    schema = str((arm_json or {}).get("$schema") or "").lower()
    for marker, scope in _SCHEMA_SCOPE_MARKERS:
        if marker in schema:
            return scope
    return SCOPE_RESOURCE_GROUP


def resolve_stack_location(arm_params):
    """
    Deployment-metadata location for a subscription-scoped stack/what-if: the
    first STACK_LOCATION_PARAM_CANDIDATES parameter that carries a non-empty
    string value, else DEFAULT_STACK_LOCATION. Never None.
    """
    for pname in STACK_LOCATION_PARAM_CANDIDATES:
        wrapped = (arm_params or {}).get(pname)
        val = wrapped.get("value") if isinstance(wrapped, dict) else None
        if isinstance(val, str) and val.strip():
            return val.strip()
    return DEFAULT_STACK_LOCATION


# ===========================================================================
# U3 — Param assembly/validation, ARM stack client, run engine
# ===========================================================================


def validate_value(name, spec, value):
    """Return an error string if value violates the parameter's constraints, else None."""
    allowed = spec.get("allowed")
    if allowed is not None and value not in allowed and not spec.get("complex"):
        return f"'{name}'='{value}' is not one of the allowed values {allowed}."
    if spec.get("min_value") is not None or spec.get("max_value") is not None:
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return f"'{name}' must be an integer."
        if spec.get("min_value") is not None and iv < spec["min_value"]:
            return f"'{name}'={iv} is below minValue {spec['min_value']}."
        if spec.get("max_value") is not None and iv > spec["max_value"]:
            return f"'{name}'={iv} is above maxValue {spec['max_value']}."
    if spec.get("min_length") is not None or spec.get("max_length") is not None:
        length = len(value) if hasattr(value, "__len__") else None
        if length is None:
            return f"'{name}' has no measurable length for a length constraint."
        if spec.get("min_length") is not None and length < spec["min_length"]:
            return f"'{name}' length {length} is below minLength {spec['min_length']}."
        if spec.get("max_length") is not None and length > spec["max_length"]:
            return f"'{name}' length {length} is above maxLength {spec['max_length']}."
    return None


def assemble_parameters(schema, pinned, supplied):
    """
    Merge pinned values + supplied (order inputs / CF mirrors) into the ARM
    `parameters` payload, intersected against the schema. Returns the ARM
    parameters dict ({name: {"value": ...}}). Raises BicepEngineError listing
    every problem (missing required, constraint violation) BEFORE anything is
    submitted to Azure. Undeclared supplied keys are dropped with a log.
    Expression-default pins are sent as nothing (omitted) so ARM evaluates them.
    """
    merged = {}
    merged.update({k: v for k, v in (supplied or {}).items()})
    # Pinned overrides supplied; an expression-pin maps to an omit sentinel.
    OMIT = object()
    for k, v in (pinned or {}).items():
        merged[k] = OMIT if v == "__bicep_omit_expression_default__" else v

    arm_params = {}
    errors = []
    for name, value in merged.items():
        if name not in schema:
            logger.debug(f"Dropping undeclared parameter '{name}' (not in template).")
            continue
        if value is OMIT:
            continue  # expression default — ARM evaluates it
        spec = schema[name]
        atype = spec.get("type", "")
        if spec.get("complex") and isinstance(value, str):
            # object/array/UDT inputs arrive as JSON text — parse to native.
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                errors.append(f"'{name}' must be valid JSON for its object/array type.")
                continue
        elif atype == "int" and isinstance(value, str):
            # STR custom-field mirrors must be coerced back to int before ARM
            # sees them, or an int param is submitted as a string on day-2.
            try:
                value = int(value.strip())
            except (TypeError, ValueError):
                errors.append(f"'{name}' must be an integer.")
                continue
        elif atype == "bool" and isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes")
        err = validate_value(name, spec, value)
        if err:
            errors.append(err)
            continue
        arm_params[name] = {"value": value}

    missing = [
        n for n, s in schema.items()
        if s["required"] and n not in arm_params
    ]
    if missing:
        errors.append(f"Missing required parameter(s): {', '.join(sorted(missing))}.")
    if errors:
        raise BicepEngineError(
            "Parameter validation failed before any Azure submission:\n  - "
            + "\n  - ".join(errors)
        )
    return arm_params


def redact_secure(value_map, schema):
    """Replace values of @secure() params with [redacted] for safe job output."""
    out = {}
    for k, v in (value_map or {}).items():
        out[k] = "[redacted]" if (k in schema and schema[k].get("secure")) else v
    return out


class BicepArmClient(object):
    """
    Azure deployment-stack REST client. Pure REST over the resource handler's
    service-principal credentials; carries no job/resource coupling. The access
    token lives only in the Authorization header and is never logged or placed
    in a URL/exception.
    """

    def __init__(self, rh):
        rh = rh.cast()
        self.rh = rh
        # Subscription via serviceaccount: AzureARMHandler.serviceaccount holds
        # the subscription id. serviceaccount is handler-specific
        # (the access key on AWS handlers); confirmed Azure-subscription here.
        self.subscription_id = rh.serviceaccount
        self.tenant_id = (
            getattr(rh, "azure_tenant_id", None) or getattr(rh, "tenant_id", None)
        )
        self._token = None
        self._token_expiry = 0.0

    def _get_token(self):
        if self._token and time.time() < self._token_expiry:
            return self._token
        # Azure OAuth2 client-credentials. Endpoint/scope confirmed at author
        # time; re-verify per cardinal rule 5.
        # https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
        url = f"{AZURE_LOGIN_BASE}/{self.tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.rh.client_id,
            "client_secret": self.rh.secret,
            "scope": ARM_SCOPE,
        }
        try:
            resp = requests.post(url, data=data, timeout=HTTP_TIMEOUT_S)
            resp.raise_for_status()
        except requests.RequestException:
            # Never echo the response body — it can carry token material.
            raise BicepEngineError(
                "Failed to obtain an Azure access token for the selected "
                "environment's resource handler. Check the service-principal "
                "credentials (client id/secret/tenant)."
            )
        body = resp.json()
        self._token = body["access_token"]
        # Refresh a minute early; the approval pause can span hours, so a cached
        # token must not outlive its stated lifetime mid-deployment.
        self._token_expiry = time.time() + int(body.get("expires_in", 3600)) - 60
        return self._token

    def _request(self, method, url, **kwargs):
        """HTTP with bearer auth; strips Authorization before any logging/re-raise."""
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._get_token()}"
        kwargs.setdefault("timeout", HTTP_TIMEOUT_S)
        resp = requests.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 429:
            # Honor Retry-After once before failing on ARM throttling.
            retry_after = resp.headers.get("Retry-After", "")
            try:
                delay = min(int(retry_after), 60) if retry_after else 10
            except ValueError:
                delay = 10
            time.sleep(delay)
            resp = requests.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            # ARM error text names the failure (a constraint, a validation error,
            # etc.) and carries no token material, so surface it. The status code
            # is stashed on the exception so callers can tell a 4xx validation
            # rejection from a transient 5xx.
            arm_err = self._format_arm_error_response(resp)
            logger.debug(f"ARM {method} {url.split('?')[0]} -> {resp.status_code}")
            detail = arm_err or "see the resource group's activity log for detail"
            exc = BicepEngineError(f"Azure request failed ({resp.status_code}): {detail}")
            exc.status_code = resp.status_code
            raise exc
        return resp

    @staticmethod
    def _format_arm_error(err):
        """Flatten an ARM error object {code, message, details[]} into one
        readable string. ARM error text describes the failure and contains no
        token material, so it is safe to surface to (group-visible) job output."""
        if not isinstance(err, dict):
            return ""
        code = err.get("code", "")
        msg = err.get("message", "")
        parts = []
        if msg:
            parts.append(f"{msg} (Code: {code})" if code else msg)
        elif code:
            parts.append(f"Code: {code}")
        for d in (err.get("details") or []):
            sub = BicepArmClient._format_arm_error(d)
            if sub:
                parts.append(sub)
        return " | ".join(parts)

    def _format_arm_error_response(self, resp):
        try:
            return self._format_arm_error(resp.json().get("error") or {})
        except (ValueError, AttributeError):
            return ""

    def _scope_prefix(self, rg):
        """ARM scope path for a stack/deployment: the resource group when `rg`
        is given, else the subscription (subscription-scoped templates)."""
        base = f"{ARM_BASE}/subscriptions/{self.subscription_id}"
        return f"{base}/resourceGroups/{rg}" if rg else base

    def _stack_url(self, rg, name):
        """`rg` falsy => subscription-scoped stack URL.
        RG:  https://learn.microsoft.com/en-us/rest/api/resources/deployment-stacks/get-at-resource-group
        Sub: https://learn.microsoft.com/en-us/rest/api/resources/deployment-stacks/get-at-subscription
        (both api-version 2024-03-01; re-verify per cardinal rule 5)."""
        return (
            f"{self._scope_prefix(rg)}/providers/Microsoft.Resources/"
            f"deploymentStacks/{name}?api-version={DEPLOYMENT_STACKS_API_VERSION}"
        )

    def get_stack(self, rg, name):
        """Return the stack object, or None on 404. `rg=None` reads a
        subscription-scoped stack (see _stack_url for the cited references).
        """
        url = self._stack_url(rg, name)
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_S)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise BicepEngineError(f"Failed to read stack '{name}' ({resp.status_code}).")
        return resp.json()

    def provisioning_state(self, rg, name):
        stack = self.get_stack(rg, name)
        if not stack:
            return None
        return (stack.get("properties") or {}).get("provisioningState")

    def create_or_update_stack(self, rg, name, arm_template, arm_params,
                               tags=None, description=None,
                               deny_mode=DENY_SETTINGS_MODE,
                               action_on_unmanage=None, location=None):
        """
        PUT the deployment stack at resource-group scope (`rg` given) or
        subscription scope (`rg` None). Body shapes per
        https://learn.microsoft.com/en-us/rest/api/resources/deployment-stacks/create-or-update-at-resource-group
        https://learn.microsoft.com/en-us/rest/api/resources/deployment-stacks/create-or-update-at-subscription
        (the subscription form REQUIRES a top-level `location`; the RG form
        inherits the RG's). Returns the response JSON.

        actionOnUnmanage default: resources are always deleted. Resource groups
        are DETACHED for an RG-scoped stack (the engine owns resources, not the
        customer's RG) but DELETED for a subscription-scoped one — there the
        template itself created the RGs, so the stack owns them and removing
        one from the template (or deleting the stack) must remove it from Azure.
        """
        if action_on_unmanage is None:
            action_on_unmanage = {
                "resources": "delete",
                "resourceGroups": "detach" if rg else "delete",
                "managementGroups": "detach",
            }
        deny_settings = {"mode": deny_mode, "applyToChildScopes": False,
                         "excludedPrincipals": [], "excludedActions": []}
        if deny_mode and deny_mode != "none":
            self._assert_self_excluded(deny_settings)
        body = {
            "properties": {
                "actionOnUnmanage": action_on_unmanage,
                "denySettings": deny_settings,
                "template": arm_template,
                "parameters": arm_params,
            }
        }
        if not rg:
            body["location"] = location or DEFAULT_STACK_LOCATION
        if tags:
            body["tags"] = tags
        if description:
            body["properties"]["description"] = description
        url = self._stack_url(rg, name)
        resp = self._request("PUT", url, json=body)
        return resp.json()

    def _assert_self_excluded(self, deny_settings):
        """
        When deny settings are active, the engine's own SP must be in
        excludedPrincipals or the engine locks itself out of its own stack.
        Derive the SP object id at runtime from the token claims (not a
        hand-entered config value, so SP rotation can't silently invalidate it).
        """
        oid = self._sp_object_id()
        if not oid:
            raise BicepEngineError(
                "Deny settings are enabled but the engine service-principal "
                "object id could not be derived; refusing to create a stack the "
                "engine could not later update. Disable deny settings or fix the "
                "credential."
            )
        if oid not in deny_settings["excludedPrincipals"]:
            deny_settings["excludedPrincipals"].append(oid)

    def _sp_object_id(self):
        """Best-effort SP object id from the access-token 'oid' claim."""
        token = self._get_token()
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            import base64
            claims = json.loads(base64.urlsafe_b64decode(payload))
            return claims.get("oid")
        except Exception:
            return None

    def delete_stack(self, rg, name, mode="deleteAll"):
        """
        DELETE the stack with delete semantics. The unmanage-action query
        parameter names vary by api-version — confirm against the current REST
        delete reference at the call site (cardinal rule 5). 'deleteAll' deletes
        managed resources; we never force-delete.
        """
        base = self._stack_url(rg, name)
        # https://learn.microsoft.com/en-us/rest/api/resources/deployment-stacks/delete-at-resource-group
        # https://learn.microsoft.com/en-us/rest/api/resources/deployment-stacks/delete-at-subscription
        # (api-version 2024-03-01; identical unmanageAction.* query params at
        # both scopes). Verify the exact names against the current delete
        # reference per cardinal rule 5. For a subscription-scoped stack,
        # ResourceGroups=delete is what removes the RGs the template created.
        sep = "&"
        if mode == "deleteAll":
            qp = (f"{sep}unmanageAction.Resources=delete"
                  f"{sep}unmanageAction.ResourceGroups=delete"
                  f"{sep}unmanageAction.ManagementGroups=detach")
        elif mode == "deleteResources":
            qp = (f"{sep}unmanageAction.Resources=delete"
                  f"{sep}unmanageAction.ResourceGroups=detach")
        else:
            qp = (f"{sep}unmanageAction.Resources=detach"
                  f"{sep}unmanageAction.ResourceGroups=detach")
        resp = self._request("DELETE", base + qp)
        return resp.status_code

    def what_if(self, rg, deployment_name, arm_template, arm_params,
                location=None):
        """
        Run a deployment what-if (NOT stack-aware — stacks have no what-if yet)
        at resource-group scope (`rg` given) or subscription scope (`rg` None;
        the request then carries the deployment-metadata `location`).
        Returns {"changes": <list|None>, "error": <str|None>}: `changes` is the
        predicted change list (possibly empty) on success, None when the preview
        was merely unavailable; `error` carries Azure's reason when what-if
        DEFINITIVELY rejected the change (a 4xx validation error or a Failed
        result) so the caller can fail before applying, and is None otherwise.

        The 202 carries a `Location` header pointing at the RESULT endpoint and
        usually an `Azure-AsyncOperation` header pointing at a STATUS-ONLY
        endpoint. The change set lives ONLY in the result (`properties.changes`)
        — the status endpoint returns just `{"status": ...}`. Reading the status
        endpoint instead of the result is why a what-if would report zero changes
        for a real diff, so we poll to completion and then read Location.
        https://learn.microsoft.com/en-us/rest/api/resources/deployments/what-if
        https://learn.microsoft.com/en-us/rest/api/resources/deployments/what-if-at-subscription-scope
        (api-version 2025-04-01; WhatIfOperationResult.properties.changes;
        re-verify per cardinal rule 5).
        """
        url = (f"{self._scope_prefix(rg)}/providers/Microsoft.Resources/"
               f"deployments/{deployment_name}/whatIf"
               f"?api-version={WHATIF_API_VERSION}")
        body = {"properties": {"mode": "Incremental", "template": arm_template,
                               "parameters": arm_params}}
        if not rg:
            body["location"] = location or DEFAULT_STACK_LOCATION
        try:
            resp = self._request("POST", url, json=body)
        except BicepEngineError as e:
            sc = getattr(e, "status_code", None)
            if sc and 400 <= sc < 500:
                # Definitive validation rejection (e.g. an unsupported change) —
                # surface the reason so the caller fails before a doomed apply.
                return {"changes": None, "error": str(e)}
            logger.warning("what-if request failed transiently; no preview.")
            return {"changes": None, "error": None}
        try:
            result = self._await_whatif_result(resp)
        except BicepEngineError:
            logger.warning("what-if did not complete; proceeding without preview.")
            return {"changes": None, "error": None}
        if result is None:
            return {"changes": None, "error": None}
        status = (result.get("status") or "").lower()
        if status in ("failed", "canceled"):
            err = self._format_arm_error(result.get("error") or {})
            return {"changes": None, "error": err or f"what-if {status}"}
        props = result.get("properties") or {}
        changes = props.get("changes")
        if changes is None:
            changes = result.get("changes")
        return {"changes": changes if changes is not None else [], "error": None}

    def managed_resource_diff(self, rg, name, arm_template):
        """
        Diff the stack's currently-managed resources against the new template to
        surface deletions what-if cannot see. Returns:
          {"deletions": [resource ids], "enumerable": True/False, "first": bool}
        Degenerate cases:
          * no stack yet (first deploy)      -> enumerable True, deletions [], first True
          * existing stack, list unreadable  -> enumerable False (caller must NOT
                                                treat as zero-deletions on day-2)
        """
        stack = self.get_stack(rg, name)
        if stack is None:
            return {"deletions": [], "enumerable": True, "first": True}
        try:
            managed = (stack.get("properties") or {}).get("resources") or []
            managed_ids = {r.get("id", "").lower() for r in managed if r.get("id")}
        except (AttributeError, TypeError):
            return {"deletions": [], "enumerable": False, "first": False}
        # Template resource names/types aren't full resource ids pre-deploy; a
        # precise diff requires resolving template resource ids. As a
        # conservative signal we report the managed set and let the caller
        # render it; exact id resolution is verified at the call site.
        template_types = {
            r.get("type", "").lower()
            for r in (arm_template.get("resources") or [])
        }
        deletions = [
            rid for rid in managed_ids
            if not any(t and t in rid for t in template_types)
        ]
        return {"deletions": deletions, "enumerable": True, "first": False}

    def stack_error_detail(self, rg, name):
        """Best-effort human-readable error for a failed stack: the stack's own
        properties.error, each failedResources[].error, and — drilling into the
        underlying deployment — its failed operation errors, which is where a
        provider constraint like StorageAccountTypeConversionNotAllowed lives."""
        stack = self.get_stack(rg, name)
        if not stack:
            return ""
        props = stack.get("properties") or {}
        msgs = []
        top = self._format_arm_error(props.get("error") or {})
        if top:
            msgs.append(top)
        for fr in (props.get("failedResources") or []):
            fe = self._format_arm_error(fr.get("error") or {})
            if fe:
                rid = (fr.get("id") or "").split("/")[-1]
                msgs.append(f"{rid}: {fe}" if rid else fe)
        dep_id = props.get("deploymentId")
        if dep_id:
            msgs.extend(self._deployment_operation_errors(dep_id))
        seen = []
        for m in msgs:
            if m and m not in seen:
                seen.append(m)
        return "; ".join(seen)

    def _deployment_operation_errors(self, deployment_id):
        """Provider error messages from a deployment's failed operations."""
        url = f"{ARM_BASE}{deployment_id}/operations?api-version={WHATIF_API_VERSION}"
        try:
            resp = self._request("GET", url)
            ops = resp.json().get("value") or []
        except (BicepEngineError, ValueError):
            return []
        out = []
        for op in ops:
            p = op.get("properties") or {}
            if (p.get("provisioningState") or "").lower() != "failed":
                continue
            sm = p.get("statusMessage")
            err = sm.get("error") if isinstance(sm, dict) else None
            f = self._format_arm_error(err or {})
            if f:
                out.append(f)
        return out

    def _await_whatif_result(self, resp):
        """Await an async what-if and return its WhatIfOperationResult body.

        Polls to a terminal status, then reads the result from the Location
        (result) endpoint — NOT the Azure-AsyncOperation (status-only) endpoint,
        which never carries `properties.changes`. A 200 POST response is already
        the result. Raises BicepEngineError on a failed/timed-out operation
        (the caller treats that as "preview unavailable").
        """
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                return None
        location = resp.headers.get("Location")
        async_op = resp.headers.get("Azure-AsyncOperation")
        status_url = async_op or location
        result_url = location or async_op
        if not status_url:
            try:
                return resp.json()
            except ValueError:
                return None
        deadline = time.time() + WHATIF_POLL_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(WHATIF_POLL_INTERVAL_S)
            pr = self._request("GET", status_url)
            if pr.status_code == 202:
                continue  # still running
            body = pr.json() if pr.content else {}
            # If we happen to be polling the result endpoint directly, the
            # change set is already present.
            if (body.get("properties") or {}).get("changes") is not None:
                return body
            status = (body.get("status") or "").lower()
            if status == "succeeded":
                # Status-only endpoint signalled done — fetch the actual result
                # (with properties.changes) from the Location/result endpoint.
                if result_url and result_url != status_url:
                    rr = self._request("GET", result_url)
                    if rr.status_code in (200, 201) and rr.content:
                        return rr.json()
                return body
            if status in ("failed", "canceled"):
                # Return the failed body; the caller extracts body["error"]
                # (the validation reason) rather than losing it to an exception.
                return body
            if pr.status_code in (200, 201) and not status:
                return body
        raise BicepEngineError("what-if operation timed out.")

    def poll_stack_to_terminal(self, rg, name):
        """Poll the stack provisioningState to a terminal value (bounded).

        A freshly-submitted async PUT may not be readable immediately, so a
        transient None (stack not yet visible) is tolerated for a bounded number
        of consecutive reads rather than being treated as "gone" — otherwise a
        successful async create could be misreported as FAILURE, leading a user
        to delete a resource whose infrastructure actually deployed.
        """
        deadline = time.time() + STACK_POLL_TIMEOUT_S
        consecutive_none = 0
        while time.time() < deadline:
            state = self.provisioning_state(rg, name)
            if state is None:
                consecutive_none += 1
                if consecutive_none >= 6:
                    return None
                time.sleep(STACK_POLL_INTERVAL_S)
                continue
            consecutive_none = 0
            if state in STACK_TERMINAL_SUCCESS:
                return state
            if state in STACK_TERMINAL_FAILURE:
                return state
            set_progress(f"Stack {name}: {state}...")
            time.sleep(STACK_POLL_INTERVAL_S)
        raise BicepEngineError(f"Stack '{name}' did not reach a terminal state in time.")


# --- The one job-touching surface ------------------------------------------

def _import_cancel_job_exception():
    """
    Resolve CloudBolt's CancelJobException. It subclasses BaseException, so the
    reject path must catch it explicitly. Import path verified during HCP
    Terraform planning against the platform source; resolved defensively here.
    """
    try:
        from jobs.models import CancelJobException  # noqa
        return CancelJobException
    except Exception:
        try:
            from utilities.exceptions import CancelJobException  # noqa
            return CancelJobException
        except Exception:
            return None


def run_with_approval(job, client, rg, stack_name, arm_template, arm_params,
                      schema, *, tags=None, description=None,
                      auto_confirm=False, is_drift=False, location=None):
    """
    The shared what-if -> approval-gate -> apply engine. The ONLY surface that
    touches the job. Does not mutate the Resource.

    `rg` is the target resource group for a resource-group-scoped template, or
    None for a subscription-scoped one (then `location` is the deployment-
    metadata location; see resolve_stack_location).

    Flow: run what-if -> managed-resource diff -> write a redacted summary to
    job output -> (unless auto_confirm/drift) job.pause() for human approval ->
    on continue, create/update the stack and poll to terminal. On
    CancelJobException (reject) nothing was submitted (what-if is read-only) so
    there is no run to discard — return a rejected marker. Drift mode stops
    after the summary (no pause, no apply).

    Approved-artifact integrity: the arm_template/arm_params passed in are the
    exact artifacts that get submitted after the pause — the caller compiles
    ONCE before calling and does not re-fetch/re-compile across the pause.

    Returns one of: {"status": "succeeded"|"failed"|"rejected"|"invalid"|
                     "drift", "changes": [...], "error": <str|None>}. "invalid"
                     means what-if rejected the change before any apply was
                     attempted; "failed" carries the real Azure error in "error".
    """
    # 1. what-if preview. Returns {"changes": <list|None>, "error": <str|None>}.
    wi = client.what_if(rg, f"{stack_name}-whatif", arm_template, arm_params,
                        location=location)
    changes = wi.get("changes")
    wi_error = wi.get("error")

    # 2. stack-deletion diff (compensates for what-if's stack blindness).
    diff = client.managed_resource_diff(rg, stack_name, arm_template)

    # 3. summary to job output (secure values redacted).
    _write_summary(changes, diff, redact_secure(
        {k: v.get("value") for k, v in arm_params.items()}, schema),
        whatif_error=wi_error)

    if is_drift:
        # Preserve None (what-if unavailable) so the caller reports UNKNOWN
        # rather than a false "no drift".
        return {"status": "drift", "changes": changes,
                "whatif_available": changes is not None, "error": wi_error}

    # If what-if definitively rejected the change (e.g. an unsupported Azure
    # operation like a SKU conversion), surface the reason now and do NOT attempt
    # a doomed apply — the user gets a clear "this change isn't possible" message
    # instead of a failed deployment with partial state.
    if wi_error:
        return {"status": "invalid", "changes": None, "error": wi_error}

    # Is there anything to review? An unavailable preview (None) or an
    # unreadable managed-resource list counts as "uncertain" -> pause, so a
    # what-if miss never bypasses approval. Only a confirmed zero-diff skips the
    # pause (nothing to approve).
    has_reviewable = (changes is None or bool(changes)
                      or bool(diff.get("deletions"))
                      or not diff.get("enumerable", True))

    if not auto_confirm and has_reviewable:
        # 4. pause for human approval. continue = approve, cancel = reject.
        CancelJobException = _import_cancel_job_exception()
        try:
            job.pause()
        except BaseException as e:  # noqa: BLE001 — CancelJobException is BaseException
            if CancelJobException and isinstance(e, CancelJobException):
                set_progress("Change rejected at approval gate; nothing submitted.")
                return {"status": "rejected", "changes": changes or []}
            raise

    # 5. apply: ALWAYS create/update the stack and poll to terminal. A true
    # no-op is an idempotent PUT; applying unconditionally guarantees the
    # resource converges even when what-if under-reports (it has documented
    # false-negatives), so a real change can never be silently skipped.
    set_progress(f"Applying deployment stack {stack_name}...")
    client.create_or_update_stack(
        rg, stack_name, arm_template, arm_params, tags=tags,
        description=description, location=location)
    state = client.poll_stack_to_terminal(rg, stack_name)
    if state in STACK_TERMINAL_SUCCESS:
        return {"status": "succeeded", "changes": changes or []}
    # Pull the real Azure error off the failed stack so the caller can show it.
    error = client.stack_error_detail(rg, stack_name)
    return {"status": "failed", "changes": changes or [], "state": state,
            "error": error}


def _write_summary(changes, diff, redacted_params, whatif_error=None):
    """Write counts-first, secret-free change summary to group-visible job output."""
    lines = ["Bicep deployment preview:"]
    if whatif_error:
        lines.append(f"  what-if rejected this change: {whatif_error}")
    elif changes is None:
        lines.append("  (what-if preview unavailable — proceeding without it)")
    else:
        counts = {}
        for c in changes:
            ct = c.get("changeType", "Unknown")
            counts[ct] = counts.get(ct, 0) + 1
        if counts:
            lines.append("  Changes: " + ", ".join(
                f"{k}={v}" for k, v in sorted(counts.items())))
        else:
            lines.append("  Changes: none")
    if not diff.get("enumerable", True):
        lines.append("  WARNING: the stack's managed-resource list could not be "
                     "read — possible deletions are NOT shown. Confirm carefully.")
    elif diff.get("deletions"):
        lines.append("  Stack-managed resources whose type is absent from the "
                     "new template (review carefully — this is a type-presence "
                     "heuristic; exact per-resource deletion is determined by "
                     "Azure on apply):")
        for rid in diff["deletions"]:
            lines.append(f"    - {rid}")
    lines.append("  Parameters: " + json.dumps(redacted_params, default=str))
    set_progress("\n".join(lines))
