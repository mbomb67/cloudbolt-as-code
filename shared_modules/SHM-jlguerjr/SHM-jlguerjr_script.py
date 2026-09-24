"""
CloudBolt shared module: HCP Terraform (TFC) REST client + run engine.

Imported by the HCP Terraform VM blueprint's build/teardown plugins and the
"Terraform Update" / "Resize" day-2 plugins as:

    from shared_modules.tfc_api import (
        get_client,
        run_with_plan_approval,
        build_run_message,
        parse_job_id_from_run_message,
        TFCError,
    )

Layering (strict -- keeps a future client/engine split cheap):

- ``TFCClient`` is a pure REST client over HCP Terraform's JSON:API v2. It
  carries ZERO CloudBolt references: explicit constructor args (host, token),
  stdlib logging by default, no job/resource/model access. The team token
  appears only in the session's Authorization header -- never in URLs,
  exception messages, or logged request dumps.
- ``get_client()`` / ``get_options_client()`` are the CloudBolt-importing
  seams: they resolve a ConnectionInfo carrying the ``tf-cloud`` label
  (``CONNECTION_INFO_LABEL``) -- by the caller-supplied reference (global_id
  or name), or unambiguously when exactly one labeled ConnectionInfo exists --
  and build the client. ``get_options_client`` returns an organization-less
  client for the order form's ``generate_options_for_*`` lookups
  (list_organizations / list_projects / list_workspace_repo_identifiers).
- ``run_with_plan_approval()`` is the ONLY surface that touches the CloudBolt
  ``job`` (plan-approval pause via ``job.pause()``, progress reporting, and
  the cancel-as-reject path). It NEVER touches the resource -- custom-field
  mutation stays in the calling plugins.

Reject-path ownership: the engine -- and only the engine -- catches
``CancelJobException`` (which subclasses ``BaseException``, so a bare
``except Exception`` will NOT see it), discards the TFC run, invokes the
caller's optional ``on_reject`` callback, then re-raises so the cancellation
completes. Plugins must never catch ``CancelJobException`` themselves.

Vendor API references (per docs/agents/external-apis.md, each call site below
also cites its doc):

- API conventions / rate limiting / pagination:
  https://developer.hashicorp.com/terraform/cloud-docs/api-docs
- Organizations (list):
  https://developer.hashicorp.com/terraform/cloud-docs/api-docs/organizations
- Workspaces (create/read/safe-delete, vcs-repo, project, tag-bindings):
  https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces
- Workspace variables:
  https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspace-variables
- Runs (create, is-destroy, actions, status_group filter):
  https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run
- Run states:
  https://developer.hashicorp.com/terraform/cloud-docs/run/states
- Plans (resource counts, log-read-url):
  https://developer.hashicorp.com/terraform/cloud-docs/api-docs/plans
- State version outputs:
  https://developer.hashicorp.com/terraform/cloud-docs/api-docs/state-version-outputs
- Projects / OAuth clients / OAuth tokens:
  https://developer.hashicorp.com/terraform/cloud-docs/api-docs/projects
  https://developer.hashicorp.com/terraform/cloud-docs/api-docs/oauth-clients
  https://developer.hashicorp.com/terraform/cloud-docs/api-docs/oauth-tokens
- Configuration versions:
  https://developer.hashicorp.com/terraform/cloud-docs/api-docs/configuration-versions
- Canonical 429 backoff semantics (X-RateLimit-Reset + jitter):
  https://github.com/hashicorp/go-tfe
"""

import ast
import json
import logging
import random
import re
import time
from urllib.parse import urljoin, urlsplit

import requests

# CloudBolt platform imports -- used ONLY by the CloudBolt seam at the bottom
# of this file (get_client / run_with_plan_approval / the custom-field
# helpers). Nothing above that seam may reference these names: TFCClient and
# the helpers must stay pure so the REST client can later be split into its
# own module without surgery.
from common.methods import set_progress
from infrastructure.models import CustomField
from utilities.exceptions import CancelJobException
from utilities.logger import ThreadLogger
from utilities.models import ConnectionInfo

logger = ThreadLogger(__name__)


# =============================================================================
# == OPERATOR CONFIG BLOCK -- EDIT ME =========================================
# =============================================================================
# Non-secret HCP Terraform coordinates for this CloudBolt instance. This repo
# is config-as-code: these values are reviewable and versioned. The ONLY
# secret -- the TFC team API token -- lives in a ConnectionInfo labeled
# CONNECTION_INFO_LABEL (password field) and must be re-entered in the
# CloudBolt UI after every repo sync (export redacts secrets).
#
# Nothing here is instance-specific: the CloudBolt portal URL recorded on each
# workspace is resolved at run time from the ordering job's portal (see
# portal_url_for_job), so a sync needs no edits to this block.

# This module is BLUEPRINT-AGNOSTIC: the TFC coordinates that pin a blueprint
# to a specific org/project/repo (organization, project, VCS repo/branch/
# working directory) and the per-template variable/output name sets are NOT
# defined here. They are pinned per blueprint (parameter_defaults on the build
# deployment item), read by the plugins, and passed into the client methods as
# arguments -- so one tfc_api module serves many blueprints, each pinned to its
# own Terraform config. See docs/hcp-terraform-setup.md and the build plugin.

# Display label recorded on each workspace as "source-name" next to the
# portal URL ("source-url"), so TFC users can navigate back to the owning CMP.
WORKSPACE_SOURCE_NAME = "CloudBolt CMP"

# CloudBolt ConnectionInfos for Terraform Cloud/Enterprise are selected by
# LABEL, not by a hard-coded name: every ConnectionInfo that carries the
# "tf-cloud" label (protocol https, ip app.terraform.io or the TFE host,
# port 443, TEAM token in the password field) is offered on the order form's
# Connection dropdown. The name itself is free-form.
CONNECTION_INFO_LABEL = "tf-cloud"
TFC_DEFAULT_HOST = "app.terraform.io"

# Workspace naming + tagging. Workspaces are named from the CloudBolt
# resource's immutable global_id only (no mutable component), and tagged with
# that global_id so retries can verify ownership before adopting a workspace.
WORKSPACE_NAME_PREFIX = "cb-vm-"
WORKSPACE_NAME_MAX_LENGTH = 90  # conservative cap; names stay ~18 chars anyway
RESOURCE_ID_TAG_KEY = "cmp:resource-id"
# No-code deployments get a distinct prefix so they are visually separable from
# the VCS blueprint's cb-vm-* workspaces in the TFC UI. Same immutable-global_id
# basis; see no_code_workspace_name_for_resource. Ownership of a no-code
# workspace is proven by this deterministic name (the no-code create ignores a
# tag-bindings relationship -- U1), with the cmp:resource-id tag confirmatory.
NO_CODE_WORKSPACE_NAME_PREFIX = "cb-nc-"

# Plan-log presentation: keep at most this many lines, preserving the END of
# the log (terraform's change summary lives in the final lines), and drop any
# line matching a secret pattern below before it reaches job output.
PLAN_LOG_MAX_LINES = 200
PLAN_LOG_SECRET_PATTERNS = [
    r"ARM_CLIENT_SECRET",
    r"ARM_CLIENT_ID",
    r"ARM_TENANT_ID",
    r"ARM_SUBSCRIPTION_ID",
    r"(?i)\bsensitive\b",
]

# Hosts the pre-signed plan-log URL ("log-read-url") may point at. The URL is
# fetched WITHOUT the bearer token, redirects are never auto-followed, and
# every hop is re-validated against this allowlist.
# Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/plans
#       (the documented log-read-url sample points at
#       https://archivist.terraform.io/v1/object/...; API calls themselves
#       go to app.terraform.io).
PLAN_LOG_HOST_ALLOWLIST = ["app.terraform.io", "archivist.terraform.io"]
_PLAN_LOG_HOST_ALLOWLIST_SET = frozenset(h.lower() for h in PLAN_LOG_HOST_ALLOWLIST)
PLAN_LOG_MAX_REDIRECTS = 3

# Bounded timeouts -- no loop in this module may run unbounded.
HTTP_REQUEST_TIMEOUT_SECONDS = 60
CONFIG_VERSION_TIMEOUT_SECONDS = 600   # VCS ingestion after workspace create
PLAN_PHASE_TIMEOUT_SECONDS = 1800      # create-run -> confirmable/terminal
APPLY_PHASE_TIMEOUT_SECONDS = 3600     # apply -> terminal
OUTPUTS_TIMEOUT_SECONDS = 600          # state-version output processing
INITIAL_RUN_TIMEOUT_SECONDS = 120      # no-code create -> auto-queued run appears
POLL_INTERVAL_SECONDS = 10

# 429 handling: bounded retries, honoring X-RateLimit-Reset plus jitter.
RATE_LIMIT_MAX_RETRIES = 8
RATE_LIMIT_MIN_BACKOFF_SECONDS = 1.0
RATE_LIMIT_JITTER_SECONDS = 2.0

# List-endpoint pagination (organizations / projects / workspaces): page size
# and a hard page cap so a huge TFC account cannot turn an order-form dropdown
# lookup into an unbounded crawl. 20 pages x 100 = 2000 items per list.
LIST_PAGE_SIZE = 100
LIST_MAX_PAGES = 20
# =============================================================================
# == END OPERATOR CONFIG BLOCK ================================================
# =============================================================================


# -----------------------------------------------------------------------------
# Exceptions (pure -- safe to import anywhere)
# -----------------------------------------------------------------------------

class TFCError(Exception):
    """Base error for every HCP Terraform client/engine failure."""


class TFCConfigError(TFCError):
    """Operator-actionable misconfiguration (config block / ConnectionInfo)."""


class TFCAuthError(TFCError):
    """401/403 from TFC -- the team token was rejected or lacks access."""


class TFCNotFoundError(TFCError):
    """404 from TFC -- the addressed object does not exist (or no access)."""


class TFCConflictError(TFCError):
    """409 from TFC -- state changed underneath us (re-read and re-branch)."""


class TFCValidationError(TFCError):
    """422 from TFC -- payload rejected (duplicate name/key, bad value...)."""


class TFCTimeoutError(TFCError):
    """A bounded poll loop hit its deadline before reaching a usable state."""


class TFCRunFailedError(TFCError):
    """A TFC run ended in a terminal non-success state. Carries the run URL."""

    def __init__(self, message, run_url=None):
        super().__init__(message)
        self.run_url = run_url


# -----------------------------------------------------------------------------
# Run-status classification (pure)
# -----------------------------------------------------------------------------
# Classification mirrors the plan's run-status table. The decision gate is
# actions.is-confirmable, NOT a specific status: cost estimation and policy
# stages are conditionally present, so "the status right before confirmable"
# is not stable across organizations.
# Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run
#       https://developer.hashicorp.com/terraform/cloud-docs/run/states

RUN_CLASS_IN_FLIGHT = "in_flight"
RUN_CLASS_CONFIRMABLE = "confirmable"
RUN_CLASS_APPLIED = "applied"
RUN_CLASS_NO_CHANGES = "planned_and_finished"
RUN_CLASS_ABANDONED = "abandoned"
RUN_CLASS_ERRORED = "errored"
RUN_CLASS_POLICY_OVERRIDE = "policy_override"

TERMINAL_ABANDONED_STATUSES = frozenset({"discarded", "canceled", "force_canceled"})

# Full in-flight status list per the Runs API doc ("planned_and_saved" only
# occurs for save-plan runs, which this module never creates; it is listed so
# an unexpected sighting polls toward the bounded timeout instead of warning
# every iteration).
KNOWN_IN_FLIGHT_STATUSES = frozenset({
    "pending", "fetching", "fetching_completed",
    "pre_plan_running", "pre_plan_completed",
    "queuing", "plan_queued", "planning", "planned",
    "cost_estimating", "cost_estimated",
    "policy_checking", "policy_checked", "policy_soft_failed",
    "post_plan_running", "post_plan_completed",
    "confirmed", "apply_queued", "applying",
    "planned_and_saved",
})

# Statuses meaning the apply has been confirmed and is underway or done -- the
# point of no return. Past here, a cancel/timeout cannot undo the apply, so the
# reject path must NOT discard the run or revert workspace variables (doing so
# would diverge the workspace from the state TFC is actively applying).
APPLY_STARTED_STATUSES = frozenset({"confirmed", "apply_queued", "applying", "applied"})


def classify_run(run_data):
    """
    Map one Runs-API document (the ``data`` dict from GET /runs/:id) to a
    RUN_CLASS_* constant. Terminal statuses win; then policy_override; then
    the actions.is-confirmable gate; everything else keeps polling.
    """
    attributes = run_data.get("attributes", {}) or {}
    status = attributes.get("status", "")
    actions = attributes.get("actions", {}) or {}

    if status == "applied":
        return RUN_CLASS_APPLIED
    if status == "planned_and_finished":
        # Success-with-no-changes everywhere (no-op day-2 submits, destroy of
        # an empty state, provision retry against converged infrastructure).
        return RUN_CLASS_NO_CHANGES
    if status in TERMINAL_ABANDONED_STATUSES:
        return RUN_CLASS_ABANDONED
    if status == "errored":
        return RUN_CLASS_ERRORED
    if status == "policy_override":
        # Out of POC scope -- callers fail with guidance to resolve in TFC.
        return RUN_CLASS_POLICY_OVERRIDE
    if actions.get("is-confirmable"):
        return RUN_CLASS_CONFIRMABLE
    return RUN_CLASS_IN_FLIGHT


# -----------------------------------------------------------------------------
# Pure helpers
# -----------------------------------------------------------------------------

def _safe_text(text, limit=600):
    """
    Sanitize external text for inclusion in CloudBolt messages: strip braces
    (CloudBolt's synchronous action path runs messages through format_html /
    str.format, where literal braces crash -- see SHM-5hjzm9e4) and truncate.

    Deliberate fork of SHM-5hjzm9e4's safe_message (this variant adds
    truncation) so this module keeps zero cross-SHM dependencies.
    """
    if not text:
        return ""
    text = str(text).replace("{", "(").replace("}", ")")
    if len(text) > limit:
        text = text[:limit] + "...(truncated)"
    return text


def workspace_name_for_resource(resource_global_id):
    """
    Deterministic TFC workspace name for a CloudBolt resource:
    ``cb-vm-<resource global_id>``. Built from the immutable global_id only,
    so cross-deployment collisions and rename drift are structurally
    impossible; the human VM name belongs in the workspace DESCRIPTION.

    Sanitized to TFC's documented name charset: "Workspace names can only
    include letters, numbers, -, and _."
    Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces
          (POST /organizations/:organization_name/workspaces, data.attributes.name)
    """
    return _sanitize_workspace_name(WORKSPACE_NAME_PREFIX, resource_global_id)


def _sanitize_workspace_name(prefix, resource_global_id):
    """Shared body for the deterministic workspace-name helpers: prefix the
    immutable global_id, lowercase, and sanitize to TFC's name charset."""
    raw = "{}{}".format(prefix, resource_global_id).lower()
    sanitized = re.sub(r"[^a-z0-9_-]", "-", raw)
    return sanitized[:WORKSPACE_NAME_MAX_LENGTH]


def no_code_workspace_name_for_resource(resource_global_id):
    """
    Deterministic TFC workspace name for a no-code deployment:
    ``cb-nc-<resource global_id>``. Same immutable-global_id basis and charset
    sanitization as workspace_name_for_resource; the distinct ``cb-nc-`` prefix
    keeps no-code workspaces visually separate from the VCS blueprint's
    ``cb-vm-*`` and is the load-bearing ownership signal for the no-code build
    (the no-code create ignores tag-bindings -- U1).
    """
    return _sanitize_workspace_name(NO_CODE_WORKSPACE_NAME_PREFIX, resource_global_id)


def build_run_message(job_id, resource_global_id, blueprint_name):
    """
    Compose the TFC run message. Non-PII identifiers ONLY -- CloudBolt job ID,
    resource global ID, blueprint name; never username/email/display name
    (end-user attribution stays inside CloudBolt). The leading job ID is what
    teardown's attribution-aware discard parses back out, so keep this format
    in lockstep with parse_job_id_from_run_message().
    """
    clean_blueprint = " ".join(str(blueprint_name or "").split()) or "unknown"
    return "CloudBolt job {} | resource {} | blueprint {}".format(
        job_id, resource_global_id, clean_blueprint
    )


_RUN_MESSAGE_JOB_ID_RE = re.compile(r"^CloudBolt job (\d+)\s*\|")


def parse_job_id_from_run_message(message):
    """
    Inverse of build_run_message(): return the owning CloudBolt job ID (int)
    from a run message, or None when the run was not created by this module
    (manual TFC runs, other tooling).
    """
    match = _RUN_MESSAGE_JOB_ID_RE.match(message or "")
    return int(match.group(1)) if match else None


# -----------------------------------------------------------------------------
# Form-driven variable funnel parsing (pure -- imported by the build plugin and
# the generic JSON day-2 Update plugin as
# ``from shared_modules.tfc_api import parse_params_payload, coerce_params_payload``)
# -----------------------------------------------------------------------------
# CloudBolt collects a Terraform template's variables in a SurveyJS custom form:
# template-specific fields live in a Dynamic Panel funneled into ONE generic
# ``parameters`` plugin input. Two vendor quirks force the parse shape below --
# ported from the Bicep engine's exemplar
# (shared_modules/SHM-bbswv27r/SHM-bbswv27r_script.py), which learned them the
# hard way:
#   1. CloudBolt renders a complex action-input value as a Python-repr string
#      (single-quoted), so ``ast.literal_eval`` must be tried BEFORE
#      ``json.loads`` (a JSON string with true/false/null is the fallback).
#   2. A SurveyJS Dynamic Panel always submits a ``list[dict]`` -- even pinned to
#      a single panel -- so a one-item list is unwrapped to the dict it wraps.
#
# ``require_dict`` shape decision: ONE helper with a flag, not two. The build
# funnel (default ``require_dict=False``) must unwrap the panel list; the day-2
# JSON textarea (``require_dict=True``) must REJECT a pasted array/scalar and
# demand a top-level object. Same parse pipeline, one behavioural switch at the
# coerce step -- cheaper to keep in lockstep than two near-duplicate helpers.


def coerce_params_payload(value, require_dict=False):
    """Normalize a parsed parameters payload to a flat ``dict``.

    A SurveyJS Dynamic Panel always submits a LIST of parameter-sets (it can
    hold 0..N panels), but a deployment collects a single set. A one-element
    list is unwrapped to its dict; a multi-element list is defensively merged
    (later panels win); a dict passes through unchanged.

    ``require_dict=True`` (day-2 JSON Update): the operator edits a JSON OBJECT
    of the deployment's current variable values, so a pasted array or scalar is
    a mistake -- reject it rather than silently unwrap/merge/empty it.
    """
    if isinstance(value, dict):
        return value
    if require_dict:
        raise TFCError(
            "Expected a JSON object of variable name/value pairs, but got a "
            + type(value).__name__
            + '. Paste a single JSON object, e.g. {"vm_size": "Standard_B2s"}.'
        )
    if isinstance(value, list):
        merged = {}
        for item in value:
            if isinstance(item, dict):
                merged.update(item)
        return merged
    if not value:
        return {}
    raise TFCError(
        "Parameters payload must be a JSON object or a list of objects."
    )


def parse_params_payload(raw, require_dict=False):
    """Parse a rendered ``parameters`` payload (str, dict, or list) into a dict.

    Tries ``ast.literal_eval`` first (CloudBolt renders complex values as a
    Python repr -- the AGENTS.md cardinal-rule-3 list/dict pattern), then falls
    back to ``json.loads`` for a JSON string carrying true/false/null. The
    parsed value is normalized by ``coerce_params_payload``: the build funnel
    (default) unwraps a Dynamic Panel's single-item ``list[dict]``; the day-2
    JSON Update passes ``require_dict=True`` to REQUIRE a top-level object and
    reject an array/scalar (R5).

    An empty/blank input is an empty dict in either mode. Unparseable input
    raises ``TFCError`` -- operator-actionable and WITHOUT echoing the raw
    (possibly secret-bearing) payload verbatim.
    """
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return coerce_params_payload(raw, require_dict=require_dict)
    text = str(raw).strip()
    if not text:
        return {}
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, RecursionError):
        # RecursionError: a pathologically deep literal. Fall through to
        # json.loads (also guarded) so it degrades to a clean TFCError, never
        # an uncaught traceback.
        try:
            value = json.loads(text)
        except (ValueError, TypeError, RecursionError):
            raise TFCError(
                "The parameters input is not a valid Python object literal or "
                "JSON. Provide a JSON object of variable name/value pairs."
            )
    return coerce_params_payload(value, require_dict=require_dict)


# -----------------------------------------------------------------------------
# Form-payload conventions layered on the funnel above (pure helpers, imported
# by the build and day-2 plugins alongside parse_params_payload).
# -----------------------------------------------------------------------------

# Reserved panel key naming the variables to write as SENSITIVE TFC workspace
# variables. The order form plants it as a hidden field inside the Dynamic
# Panel (e.g. a checkbox with defaultValue ["admin_password"]), so it rides
# the generic ``parameters`` funnel without a schema change. It is a MARKER,
# not a Terraform variable: the build plugin pops it (pop_sensitive_marker)
# before the payload becomes the workspace variable set.
SENSITIVE_MARKER_KEY = "_sensitive"


def pop_sensitive_marker(variables):
    """Pop the ``_sensitive`` marker from a parsed panel dict; return the names.

    Tolerant of the shapes SurveyJS/CloudBolt may deliver: a real list
    (checkbox question), a JSON-array string, a single name, or a
    comma-separated string. Returns a sorted list of unique non-blank names;
    an absent/blank marker returns []. Mutates ``variables`` -- the marker
    must never reach the workspace as a Terraform variable.
    """
    raw = variables.pop(SENSITIVE_MARKER_KEY, None)
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except ValueError:
            raw = text.split(",")
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    return sorted({str(name).strip() for name in raw if str(name).strip()})


def collapse_key_value_rows(value):
    """Collapse a SurveyJS matrixdynamic key/value row list into a dict.

    A matrixdynamic question with columns named ``key`` and ``value`` submits
    ``[{"key": ..., "value": ...}, ...]``, but a Terraform ``map(string)``
    variable wants ``{key: value}``. Collapses ONLY when the value is a
    non-empty list whose every item is a dict with no keys beyond
    {"key", "value"} -- anything else (including a genuine ``list(object)``
    variable) passes through unchanged. Rows with a blank key (added but
    never filled in) are skipped; later rows win on duplicate keys; an
    all-blank matrix collapses to {} (which callers drop, so the template
    default applies).
    """
    if not isinstance(value, list) or not value:
        return value
    rows = {}
    for item in value:
        if not isinstance(item, dict) or not set(item) <= {"key", "value"}:
            return value
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        row_value = item.get("value")
        rows[key] = "" if row_value is None else str(row_value)
    return rows


def serialize_hcl_value(value):
    """JSON-encode a dict/list for an ``hcl: true`` TFC variable write.

    JSON is valid HCL2 expression syntax (object constructors accept ``:`` as
    the key/value separator -- HCL native syntax spec, "Collection Values":
    https://github.com/hashicorp/hcl/blob/main/hclsyntax/spec.md), so a plain
    ``json.dumps`` yields an expression terraform converts natively to the
    variable's declared object/map/list type. The one hazard is template
    interpolation: an hcl:true value is EVALUATED, so ``${`` / ``%{`` inside
    a user-supplied string would execute as an HCL template. Both are escaped
    to their literal forms (``$${`` / ``%%{``) per
    https://developer.hashicorp.com/terraform/language/expressions/strings#escape-sequences
    -- closing the injection vector that previously kept this module
    hcl:false-only.
    """
    return json.dumps(value).replace("${", "$${").replace("%{", "%%{")


def serialize_variable_mirror(value):
    """Render a variable value for its ``tfc_var_<name>`` STR mirror field.

    dict/list values are stored as JSON (NOT Python repr) so the day-2
    actions can round-trip them back to native via parse_variable_mirror;
    scalars store as plain str. Keep in lockstep with parse_variable_mirror.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def parse_variable_mirror(text, is_hcl=False):
    """Parse a ``tfc_var_<name>`` mirror back to the value upserts need.

    ``is_hcl`` = the name is listed in the resource's tfc_hcl_variable_names
    (seeded by the build plugin): the mirror holds JSON -- parse it back to
    native so a day-2 full-set upsert re-writes it as an hcl:true value, not
    a quoted string. A mirror that fails to parse (written before this
    convention existed, or hand-edited) falls back to the raw string,
    degrading to the old behavior instead of crashing the action.
    """
    if not is_hcl:
        return text
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return text
    return value if isinstance(value, (dict, list)) else text


_SECRET_PATTERN_RES = [re.compile(pattern) for pattern in PLAN_LOG_SECRET_PATTERNS]


def _redact_plan_log(log_text):
    """Drop any plan-log line matching a configured secret pattern."""
    redacted_lines = []
    for line in (log_text or "").splitlines():
        if any(pattern.search(line) for pattern in _SECRET_PATTERN_RES):
            redacted_lines.append("[line removed: matched secret pattern]")
        else:
            redacted_lines.append(line)
    return "\n".join(redacted_lines)


def _tail_excerpt(text, max_lines=PLAN_LOG_MAX_LINES):
    """
    Cap text at max_lines, preserving the END of the log -- terraform's
    change summary lives in the final lines, so truncation removes lines
    from the front.
    """
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    dropped = len(lines) - max_lines
    header = "... ({} earlier line(s) truncated; full log at the TFC run URL) ...".format(dropped)
    return "\n".join([header] + lines[-max_lines:])


# Terraform prints diagnostics (warnings) in its human-readable plan log with a
# "Warning:" summary line -- e.g. "Warning: Value for undeclared variable",
# which is what a mistyped workspace-variable name produces when its real target
# still has a .tf default (the plan then SUCCEEDS on the stale default, so the
# approver would otherwise never see the typo -- see R3). In non-interactive run
# logs the summary line may be prefixed with a box-drawing gutter
# ("| Warning: ..." or the U+2502 box vertical). This matcher is a deliberately
# simple heuristic (case-insensitive "Warning:" after optional gutter chars),
# NOT a full diagnostic parser: it captures the summary line only; the full
# multi-line warning body (including the offending variable name) stays in the
# plan log at the TFC run URL.
# Docs (warning/diagnostic formatting in plan output; re-verify per cardinal
# rule 5): https://developer.hashicorp.com/terraform/cloud-docs/api-docs/plans
#          https://developer.hashicorp.com/terraform/cli/commands/plan
# Char class = an optional gutter (whitespace, U+2502 box verticals, ASCII
# pipes) before the "warning:" token.
_PLAN_LOG_WARNING_RE = re.compile(r"^[\s│|]*warning:", re.IGNORECASE)


def _extract_plan_warnings(log_text):
    """
    Return terraform's ``Warning:`` summary lines from a plan log, in order,
    joined by newlines ("" when there are none).

    Scanned from the FULL log so a warning that falls outside the tail cap is
    still surfaced; the caller applies the same secret-pattern redaction
    (``_redact_plan_log``) to the result before it reaches job output. Matcher
    is a heuristic -- see ``_PLAN_LOG_WARNING_RE``.
    """
    matches = [
        line for line in (log_text or "").splitlines()
        if _PLAN_LOG_WARNING_RE.match(line)
    ]
    return "\n".join(matches)


def _validate_log_url(url):
    """
    Validate a plan-log URL before a tokenless fetch: https scheme and a
    hostname on PLAN_LOG_HOST_ALLOWLIST. Raises TFCError (without echoing the
    pre-signed URL -- its path carries the access grant).
    """
    parts = urlsplit(url or "")
    if parts.scheme != "https":
        raise TFCError(
            "Refusing to fetch plan log: URL scheme '{}' is not https.".format(parts.scheme or "?")
        )
    hostname = (parts.hostname or "").lower()
    if hostname not in _PLAN_LOG_HOST_ALLOWLIST_SET:
        raise TFCError(
            "Refusing to fetch plan log: host '{}' is not on the allowlist {}.".format(
                hostname or "?", PLAN_LOG_HOST_ALLOWLIST
            )
        )


def _portal_url(portal):
    """https URL for a PortalConfig: its site_url when set, else its domain."""
    if portal is None:
        return ""
    url = (getattr(portal, "site_url", "") or "").strip()
    if not url:
        domain = (getattr(portal, "domain", "") or "").strip()
        url = "https://{}".format(domain) if domain else ""
    elif "://" not in url:
        url = "https://{}".format(url)
    return url.rstrip("/")


def portal_url_for_job(job):
    """
    Base URL of the CloudBolt portal the ordering user came through, for the
    workspace's "source-url" link back to CloudBolt. Walks the job's
    parent_job chain to the order item's Order and takes its portal (the
    portal the order was placed on), falling back to CloudBolt's default
    portal (PortalConfig.get_current_portal() without a request). Returns ""
    when no portal is configured; callers then omit source-url.
    """
    current = job
    seen = 0
    while current is not None and seen < 10:
        order_item = getattr(current, "order_item", None)
        order = getattr(order_item, "order", None)
        url = _portal_url(getattr(order, "portal", None))
        if url:
            return url
        current = getattr(current, "parent_job", None)
        seen += 1
    try:
        from portals.models import PortalConfig
        return _portal_url(PortalConfig.get_current_portal())
    except Exception as exc:  # noqa: BLE001 -- a missing portal must not block provisioning
        logger.debug("No portal resolved for job %s: %s", getattr(job, "id", "?"), exc)
        return ""


# -----------------------------------------------------------------------------
# Pure REST client -- ZERO CloudBolt references in this class
# -----------------------------------------------------------------------------

class TFCClient(object):
    """
    Minimal JSON:API client for HCP Terraform.

    Pure REST over explicit constructor args: ``TFCClient(host, token)``.
    The token is stored only on the requests session's Authorization header;
    it never appears in URLs, exception messages, or log output. All loops
    are bounded; 429s back off honoring X-RateLimit-Reset plus jitter.
    """

    def __init__(self, host, token, organization=None, log=None):
        if not host:
            raise TFCConfigError("TFCClient requires a host (e.g. app.terraform.io).")
        if not token:
            raise TFCConfigError(
                "TFCClient requires an API token. The token belongs in the "
                "password field of a ConnectionInfo labeled '{}'.".format(
                    CONNECTION_INFO_LABEL
                )
            )
        # organization may be None for org-independent lookups (the order
        # form's list_organizations dropdown); every org-scoped method guards
        # via _require_org().
        self.host = host
        self.organization = organization
        self._log = log or logging.getLogger(__name__)
        self._session = requests.Session()
        # Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs
        #       (all v2 endpoints prefixed /api/v2; Authorization: Bearer;
        #       Content-Type: application/vnd.api+json)
        self._base_url = "https://{}/api/v2".format(host)
        self._session.headers.update({
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/vnd.api+json",
        })

    def __repr__(self):
        # Deliberately token-free.
        return "TFCClient(host={!r}, organization={!r})".format(self.host, self.organization)

    def _require_org(self):
        """Guard for org-scoped endpoints on a client built without one."""
        if not self.organization:
            raise TFCConfigError(
                "This TFC call is organization-scoped, but the client was "
                "built without one. Select a TFC Organization first (order "
                "form), or pass the organization stored on the resource."
            )
        return self.organization

    # -- low-level ------------------------------------------------------------

    def _request(self, method, path, json_body=None, params=None, allowed_statuses=()):
        """
        Issue one API call with bounded 429 retries. Returns the response for
        2xx (and any status in ``allowed_statuses``); raises a classified
        TFCError subclass otherwise. Error text comes from the JSON:API
        ``errors[]`` objects -- never from request headers.
        """
        url = self._base_url + path
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self._session.request(
                    method, url, json=json_body, params=params,
                    timeout=HTTP_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                # str(exc) on connection errors never includes headers.
                raise TFCError(
                    "HTTP error calling TFC {} {}: {}".format(method, path, _safe_text(exc))
                )
            if response.status_code == 429 and attempts <= RATE_LIMIT_MAX_RETRIES:
                wait_seconds = self._rate_limit_backoff(response)
                self._log.warning(
                    "TFC rate limit (429) on %s %s; backing off %.1fs (attempt %d/%d).",
                    method, path, wait_seconds, attempts, RATE_LIMIT_MAX_RETRIES,
                )
                time.sleep(wait_seconds)
                continue
            if response.status_code < 400 or response.status_code in allowed_statuses:
                return response
            self._raise_for_status(response, method, path)

    @staticmethod
    def _rate_limit_backoff(response):
        """
        Compute the 429 backoff. X-RateLimit-Reset is the (possibly
        fractional) number of SECONDS until the limit resets; use it as the
        minimum wait and add random jitter, mirroring HashiCorp's own client.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs#rate-limiting
              https://github.com/hashicorp/go-tfe (tfe.go rateLimitBackoff)
        """
        try:
            reset_seconds = float(response.headers.get("X-RateLimit-Reset", ""))
        except (TypeError, ValueError):
            reset_seconds = 0.0
        minimum = max(RATE_LIMIT_MIN_BACKOFF_SECONDS, reset_seconds)
        return minimum + random.uniform(0, RATE_LIMIT_JITTER_SECONDS)

    @staticmethod
    def _error_detail(response):
        """Extract 'title: detail' pairs from a JSON:API error body."""
        try:
            errors = response.json().get("errors", [])
        except ValueError:
            errors = []
        parts = []
        for error in errors:
            title = error.get("title", "")
            detail = error.get("detail", "")
            parts.append(": ".join([piece for piece in (title, detail) if piece]))
        return _safe_text("; ".join(part for part in parts if part) or response.text)

    def _raise_for_status(self, response, method, path):
        """Map an error response to a classified exception. Token-free text."""
        status = response.status_code
        detail = self._error_detail(response)
        context = "{} {} -> HTTP {}".format(method, path, status)
        if status in (401, 403):
            raise TFCAuthError(
                "TFC rejected the API token ({}; {}). Operator action: open "
                "the selected '{}'-labeled ConnectionInfo in CloudBolt and "
                "re-enter a valid HCP Terraform team token in the password "
                "field (tokens are redacted on every repo sync), and confirm "
                "the token's team has access to {}.".format(
                    context, detail, CONNECTION_INFO_LABEL,
                    "organization '{}'".format(self.organization)
                    if self.organization else "the target organization",
                )
            )
        if status == 404:
            raise TFCNotFoundError("TFC object not found ({}; {}).".format(context, detail))
        if status == 409:
            raise TFCConflictError("TFC conflict ({}; {}).".format(context, detail))
        if status == 422:
            raise TFCValidationError("TFC rejected the payload ({}; {}).".format(context, detail))
        raise TFCError("TFC API error ({}; {}).".format(context, detail))

    def _list_all(self, path, params=None):
        """
        Collect every ``data`` item from a paginated list endpoint, bounded by
        LIST_MAX_PAGES. HCP Terraform paginates via ``page[number]`` /
        ``page[size]`` and reports the next page in ``meta.pagination``.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs#pagination
        """
        items = []
        page_number = 1
        while page_number <= LIST_MAX_PAGES:
            page_params = dict(params or {})
            page_params["page[number]"] = page_number
            page_params["page[size]"] = LIST_PAGE_SIZE
            response = self._request("GET", path, params=page_params)
            body = response.json()
            items.extend(body.get("data", []))
            pagination = (body.get("meta", {}) or {}).get("pagination", {}) or {}
            next_page = pagination.get("next-page")
            if not next_page:
                return items
            page_number = next_page
        self._log.warning(
            "TFC list %s exceeded %d pages; returning the first %d items.",
            path, LIST_MAX_PAGES, len(items),
        )
        return items

    # -- lookups --------------------------------------------------------------

    def list_organizations(self):
        """
        Names of every organization the token can see, sorted. Organization-
        independent (works on a client built without one) -- feeds the order
        form's TFC Organization dropdown.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/organizations
              (GET /organizations; attributes.name; paginated)
        """
        organizations = self._list_all("/organizations")
        names = [
            (organization.get("attributes", {}) or {}).get("name")
            for organization in organizations
        ]
        return sorted(name for name in names if name)

    def list_projects(self):
        """
        Names of every project in the client's organization, sorted -- feeds
        the order form's TFC Project dropdown.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/projects
              (GET /organizations/:organization_name/projects; paginated)
        """
        projects = self._list_all(
            "/organizations/{}/projects".format(self._require_org())
        )
        names = [
            (project.get("attributes", {}) or {}).get("name")
            for project in projects
        ]
        return sorted(name for name in names if name)

    def list_workspace_repo_identifiers(self, project_name=None):
        """
        Distinct VCS repo identifiers (``org/repo``) tracked by the
        organization's existing workspaces, sorted -- feeds the order form's
        TFC VCS Repo dropdown. When ``project_name`` resolves, workspaces are
        filtered to that project first; an empty project-scoped result falls
        back to the whole organization.

        HCP Terraform's public API has no endpoint that lists the repos a VCS
        OAuth connection COULD reach (the UI's repo picker uses an
        undocumented internal endpoint), so the documented workspaces API is
        the source of truth here: any repo already tracked by a workspace is
        offered. A brand-new repo no workspace tracks yet is seeded by
        creating one throwaway workspace on it in TFC -- see the bootstrap
        note in docs/hcp-terraform-setup.md.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces
              (GET /organizations/:organization_name/workspaces;
              filter[project][id]; attributes.vcs-repo.identifier)
        """
        organization = self._require_org()
        params = {}
        if project_name:
            try:
                params["filter[project][id]"] = self.get_project_id(project_name)
            except TFCError as exc:
                self._log.warning(
                    "Could not resolve TFC project '%s' for the repo lookup "
                    "(%s); listing repos organization-wide.", project_name, exc,
                )
        workspaces = self._list_all(
            "/organizations/{}/workspaces".format(organization), params=params
        )
        identifiers = {
            ((workspace.get("attributes", {}) or {}).get("vcs-repo") or {}).get("identifier")
            for workspace in workspaces
        }
        identifiers.discard(None)
        if not identifiers and params:
            # The selected project has no VCS-backed workspaces yet -- offer
            # the organization-wide set instead of an empty dropdown.
            return self.list_workspace_repo_identifiers(project_name=None)
        return sorted(identifiers)

    def get_project_id(self, project_name):
        """
        Resolve a TFC project ID (prj-...) by name. The project is selected
        on the order form and passed in by the caller.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/projects
              (GET /organizations/:organization_name/projects,
              filter[names] matches name case-insensitively)
        """
        response = self._request(
            "GET",
            "/organizations/{}/projects".format(self._require_org()),
            params={"filter[names]": project_name},
        )
        for project in response.json().get("data", []):
            name = (project.get("attributes", {}) or {}).get("name", "")
            if name.lower() == project_name.lower():
                return project["id"]
        raise TFCConfigError(
            "TFC project '{}' was not found in organization '{}'. Create it "
            "(see docs/hcp-terraform-setup.md) or correct the project pinned on "
            "the blueprint's build item.".format(project_name, self.organization)
        )

    def get_vcs_oauth_token_id(self):
        """
        Resolve the OAuth token ID (ot-...) used to bind workspaces to the
        VCS repo: list the org's OAuth clients, then each client's tokens.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/oauth-clients
              (GET /organizations/:organization_name/oauth-clients)
              https://developer.hashicorp.com/terraform/cloud-docs/api-docs/oauth-tokens
              (GET /oauth-clients/:oauth_client_id/oauth-tokens)
        """
        clients_response = self._request(
            "GET", "/organizations/{}/oauth-clients".format(self._require_org())
        )
        oauth_clients = clients_response.json().get("data", [])
        for oauth_client in oauth_clients:
            tokens_response = self._request(
                "GET", "/oauth-clients/{}/oauth-tokens".format(oauth_client["id"])
            )
            tokens = tokens_response.json().get("data", [])
            if tokens:
                if len(oauth_clients) > 1:
                    self._log.info(
                        "Multiple VCS OAuth clients exist in org '%s'; using "
                        "client %s.", self.organization, oauth_client["id"],
                    )
                return tokens[0]["id"]
        raise TFCConfigError(
            "No VCS OAuth token exists in TFC organization '{}'. Connect a "
            "VCS provider to the organization (see docs/hcp-terraform-setup.md) "
            "so workspaces can track the blueprint's Terraform "
            "repo.".format(self.organization)
        )

    # -- workspaces -----------------------------------------------------------

    def get_workspace(self, workspace_id):
        """
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces
              (GET /workspaces/:workspace_id)
        """
        response = self._request("GET", "/workspaces/{}".format(workspace_id))
        return response.json()["data"]

    def get_workspace_by_name(self, workspace_name):
        """
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces
              (GET /organizations/:organization_name/workspaces/:name)
        """
        response = self._request(
            "GET",
            "/organizations/{}/workspaces/{}".format(self._require_org(), workspace_name),
        )
        return response.json()["data"]

    def get_workspace_tag_bindings(self, workspace_id):
        """
        Key/value tags bound directly to a workspace.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces
              (GET /workspaces/:workspace_id/tag-bindings)
        """
        response = self._request("GET", "/workspaces/{}/tag-bindings".format(workspace_id))
        return [item.get("attributes", {}) or {} for item in response.json().get("data", [])]

    def workspace_has_resource_tag(self, workspace_id, resource_global_id):
        """
        True when the workspace carries the ``cmp:resource-id`` tag for this
        resource. Compared case-insensitively on both key and value: the tags
        doc does not promise case preservation, so TFC normalization must not
        break adoption.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/workspaces/tags
        """
        wanted_key = RESOURCE_ID_TAG_KEY.lower()
        wanted_value = str(resource_global_id).lower()
        for binding in self.get_workspace_tag_bindings(workspace_id):
            key = str(binding.get("key", "")).lower()
            value = str(binding.get("value", "")).lower()
            if key == wanted_key and value == wanted_value:
                return True
        return False

    def ensure_workspace(self, resource_global_id, project_name, repo_identifier,
                         branch, working_directory="", description="",
                         stored_workspace_id=None, source_url=""):
        """
        Get-or-adopt the deployment's workspace, ID-first. The TFC project and
        the VCS repo/branch/working-directory are pinned per blueprint and
        passed in by the caller; ``source_url`` is the ordering portal's base
        URL (portal_url_for_job) recorded as the workspace's link back to
        CloudBolt. Returns the workspace document (``data`` dict; id at
        ["id"], name at ["attributes"]["name"]).

        - A stored workspace ID is GET-verified by its ``cmp:resource-id``
          tag and adopted; a tag mismatch fails loudly (foreign workspace).
          A stale stored ID (404) falls back to create-by-name.
        - Otherwise create ``cb-vm-<global_id>`` with the VM name in the
          DESCRIPTION, API-driven settings (speculative-enabled false,
          file-triggers-enabled false, queue-all-runs left at its default),
          tags set atomically in the create payload, and source-name /
          source-url pointing back at CloudBolt.
        - A 422 on create means the name already exists: GET by name, verify
          the tag (case-insensitively), adopt or fail loudly.

        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces
              (POST /organizations/:organization_name/workspaces with
              data.attributes.{name, description, speculative-enabled,
              file-triggers-enabled, working-directory, source-name,
              source-url, vcs-repo{identifier, oauth-token-id, branch}} and
              data.relationships.{project.data, tag-bindings.data})
        """
        if stored_workspace_id:
            try:
                workspace = self.get_workspace(stored_workspace_id)
            except TFCNotFoundError:
                self._log.warning(
                    "Stored TFC workspace id %s no longer exists; falling "
                    "back to create-by-name.", stored_workspace_id,
                )
            else:
                if not self.workspace_has_resource_tag(stored_workspace_id, resource_global_id):
                    raise TFCError(
                        "Stored TFC workspace id {} exists but is not tagged "
                        "{}={}. Refusing to adopt a workspace this resource "
                        "does not own; investigate in TFC before retrying.".format(
                            stored_workspace_id, RESOURCE_ID_TAG_KEY, resource_global_id,
                        )
                    )
                self._log.info(
                    "Adopting stored TFC workspace %s for resource %s.",
                    stored_workspace_id, resource_global_id,
                )
                return workspace

        workspace_name = workspace_name_for_resource(resource_global_id)
        project_id = self.get_project_id(project_name)
        oauth_token_id = self.get_vcs_oauth_token_id()
        payload = {
            "data": {
                "type": "workspaces",
                "attributes": {
                    "name": workspace_name,
                    # The mutable, human-facing VM name belongs here -- never
                    # in the workspace name (rename drift, collisions).
                    "description": description or "",
                    # API-driven only: CloudBolt creates every run explicitly.
                    # Many workspaces track one repo, so VCS events must not
                    # fan runs into every deployment workspace, and PRs must
                    # not spam speculative plans. queue-all-runs is left at
                    # its default deliberately.
                    "speculative-enabled": False,
                    "file-triggers-enabled": False,
                    "working-directory": working_directory,
                    "source-name": WORKSPACE_SOURCE_NAME,
                    "vcs-repo": {
                        "identifier": repo_identifier,
                        "oauth-token-id": oauth_token_id,
                        "branch": branch,
                    },
                },
                "relationships": {
                    "project": {"data": {"type": "projects", "id": project_id}},
                    # Tags ride the create payload atomically so no window
                    # exists where the workspace is untagged (the adoption
                    # check above depends on the tag being present).
                    "tag-bindings": {
                        "data": [
                            {
                                "type": "tag-bindings",
                                "attributes": {
                                    "key": RESOURCE_ID_TAG_KEY,
                                    "value": str(resource_global_id),
                                },
                            }
                        ]
                    },
                },
            }
        }
        if source_url:
            payload["data"]["attributes"]["source-url"] = source_url
        try:
            response = self._request(
                "POST",
                "/organizations/{}/workspaces".format(self._require_org()),
                json_body=payload,
            )
        except TFCValidationError as create_error:
            # 422: most likely "name already taken" from a previous attempt.
            try:
                existing = self.get_workspace_by_name(workspace_name)
            except TFCNotFoundError:
                # Not a name conflict -- surface the original validation error.
                raise create_error
            if self.workspace_has_resource_tag(existing["id"], resource_global_id):
                self._log.info(
                    "TFC workspace '%s' already exists with matching %s tag; "
                    "adopting it.", workspace_name, RESOURCE_ID_TAG_KEY,
                )
                return existing
            raise TFCError(
                "TFC workspace '{}' already exists but is not tagged {}={}; "
                "refusing to adopt a foreign workspace. Resolve the name "
                "collision in TFC, then retry. Original error: {}".format(
                    workspace_name, RESOURCE_ID_TAG_KEY, resource_global_id,
                    _safe_text(create_error),
                )
            )
        workspace = response.json()["data"]
        self._log.info(
            "Created TFC workspace '%s' (%s) in project '%s'.",
            workspace_name, workspace.get("id"), project_name,
        )
        return workspace

    # -- no-code provisioning -------------------------------------------------
    # These serve the HCP Terraform No-Code Module blueprint (a separate
    # blueprint from the VCS one above). They are ADDITIVE: the VCS
    # ensure_workspace/create_run path is unchanged. The no-code create makes
    # the workspace FROM a registry module and HCP auto-queues its first run;
    # the plugin adopts that run via drive_run_with_plan_approval.

    def _no_code_var_relationship(self, variables, sensitive_keys=None,
                                  env_variables=None):
        """Build the ``vars`` relationship data for a no-code workspace create,
        using the SAME value typing as upsert_variables (dict/list -> hcl:true
        JSON expression via serialize_hcl_value; scalars -> hcl:false str; keys
        in sensitive_keys -> sensitive:true). ``env_variables`` are written
        with category "env" (provider environment variables such as
        ARM_SUBSCRIPTION_ID) so the auto-queued first run already targets the
        right subscription. Kept deliberately parallel to upsert_variables --
        both are the workspace-variable write surfaces.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/no-code-provisioning
              (POST /no-code-modules/:id/workspaces, data.relationships.vars.data[]
              with attributes.category "terraform" or "env")
        """
        sensitive = frozenset(sensitive_keys or ())
        data = []
        for key in variables:
            native = variables[key]
            if isinstance(native, (dict, list)):
                value = serialize_hcl_value(native)
                hcl = True
            else:
                value = "" if native is None else str(native)
                hcl = False
            data.append({
                "type": "vars",
                "attributes": {
                    "key": key,
                    "value": value,
                    "category": "terraform",
                    "hcl": hcl,
                    "sensitive": key in sensitive,
                },
            })
        for key, native in (env_variables or {}).items():
            data.append({
                "type": "vars",
                "attributes": {
                    "key": key,
                    "value": "" if native is None else str(native),
                    "category": "env",
                    "hcl": False,
                    "sensitive": False,
                },
            })
        return data

    def get_no_code_variable_options(self, nocode_module_id):
        """Admin-defined allowed values per variable of a no-code module:
        {variable_name: [option, ...]}. Variables without options are absent.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/no-code-provisioning
              (GET /no-code-modules/:id?include=variable_options -> included[]
              of type "variable-options" with attributes.{variable-name,
              variable-type, options})
        """
        response = self._request(
            "GET",
            "/no-code-modules/{}".format(nocode_module_id),
            params={"include": "variable_options"},
        )
        options = {}
        for item in response.json().get("included", []) or []:
            if item.get("type") != "variable-options":
                continue
            attributes = item.get("attributes", {}) or {}
            name = attributes.get("variable-name")
            if name:
                options[name] = list(attributes.get("options") or [])
        return options

    def workspace_resource_tag_conflicts(self, workspace_id, resource_global_id):
        """True only when the workspace carries a ``cmp:resource-id`` tag whose
        value is a DIFFERENT resource -- the one case that must block name-based
        adoption of a no-code workspace. An ABSENT tag is NOT a conflict: the
        no-code create silently drops a tag-bindings relationship (U1), so the
        deterministic ``cb-nc-<global_id>`` name is the ownership proof and the
        tag is confirmatory only. (Contrast workspace_has_resource_tag, which
        REQUIRES the tag for the VCS stored-ID adoption path.)
        """
        wanted_key = RESOURCE_ID_TAG_KEY.lower()
        wanted_value = str(resource_global_id).lower()
        for binding in self.get_workspace_tag_bindings(workspace_id):
            key = str(binding.get("key", "")).lower()
            value = str(binding.get("value", "")).lower()
            if key == wanted_key and value != wanted_value:
                return True
        return False

    def apply_resource_tag(self, workspace_id, resource_global_id):
        """Best-effort: bind ``cmp:resource-id=<global_id>`` to the workspace
        AFTER a no-code create (the create ignores a tag-bindings relationship
        -- U1). Never raises: the deterministic workspace name is the
        load-bearing ownership signal, so a failed tag write must not fail
        provisioning. Returns True on success, False on a tolerated failure.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces
              (PATCH /workspaces/:workspace_id, data.relationships.tag-bindings.data[])
        """
        payload = {
            "data": {
                "type": "workspaces",
                "id": workspace_id,
                "relationships": {
                    "tag-bindings": {
                        "data": [
                            {
                                "type": "tag-bindings",
                                "attributes": {
                                    "key": RESOURCE_ID_TAG_KEY,
                                    "value": str(resource_global_id),
                                },
                            }
                        ]
                    },
                },
            }
        }
        try:
            self._request("PATCH", "/workspaces/{}".format(workspace_id), json_body=payload)
        except TFCError as exc:
            self._log.warning(
                "Best-effort tag %s=%s on workspace %s failed (%s); relying on "
                "the deterministic workspace name for ownership.",
                RESOURCE_ID_TAG_KEY, resource_global_id, workspace_id, _safe_text(exc),
            )
            return False
        return True

    def create_no_code_workspace(self, nocode_module_id, resource_global_id,
                                 project_name, variables, sensitive_keys=None,
                                 description="", env_variables=None, source_url=""):
        """Create a dedicated workspace FROM a no-code module; returns the
        workspace ``data`` dict (id at ["id"], name at ["attributes"]["name"]).

        ``auto-apply`` is pinned False so the run HCP auto-queues pauses at the
        confirmable gate for the plan-approval engine to adopt (U1-confirmed).
        tag-bindings are NOT sent -- the endpoint ignores them (U1) -- so the
        caller applies the tag best-effort afterward. A 422 name collision from
        a prior attempt adopts the existing workspace by name unless it carries
        a different resource-id tag (name is the ownership proof).
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/no-code-provisioning
              (POST /no-code-modules/:id/workspaces with data.attributes.{name,
              description, auto-apply, source-name, source-url} and
              data.relationships.{project.data, vars.data[]})
        """
        workspace_name = no_code_workspace_name_for_resource(resource_global_id)
        project_id = self.get_project_id(project_name)
        payload = {
            "data": {
                "type": "workspaces",
                "attributes": {
                    "name": workspace_name,
                    "description": description or "",
                    # Auto-queued run must pause, not auto-apply, so CloudBolt's
                    # engine can adopt it for human plan approval (U1).
                    "auto-apply": False,
                    "source-name": WORKSPACE_SOURCE_NAME,
                },
                "relationships": {
                    "project": {"data": {"type": "projects", "id": project_id}},
                    "vars": {
                        "data": self._no_code_var_relationship(
                            variables, sensitive_keys, env_variables=env_variables
                        )
                    },
                },
            }
        }
        if source_url:
            payload["data"]["attributes"]["source-url"] = source_url
        try:
            response = self._request(
                "POST",
                "/no-code-modules/{}/workspaces".format(nocode_module_id),
                json_body=payload,
            )
        except TFCValidationError as create_error:
            # 422: most likely the deterministic name already exists from a
            # prior attempt. Adopt by name unless a different resource owns it.
            try:
                existing = self.get_workspace_by_name(workspace_name)
            except TFCNotFoundError:
                raise create_error
            if self.workspace_resource_tag_conflicts(existing["id"], resource_global_id):
                raise TFCError(
                    "No-code workspace '{}' already exists but is tagged for a "
                    "different resource; refusing to adopt a foreign workspace. "
                    "Resolve the name collision in TFC, then retry. Original "
                    "error: {}".format(workspace_name, _safe_text(create_error))
                )
            self._log.info(
                "No-code workspace '%s' already exists (name-owned); adopting it.",
                workspace_name,
            )
            return existing
        workspace = response.json()["data"]
        self._log.info(
            "Created no-code workspace '%s' (%s) from module %s.",
            workspace_name, workspace.get("id"), nocode_module_id,
        )
        return workspace

    def wait_for_initial_run(self, workspace_id,
                             timeout_seconds=INITIAL_RUN_TIMEOUT_SECONDS,
                             progress_callback=None):
        """Return the run the no-code create auto-queues (``data`` dict) once one
        exists, or None if none appears within the bound (the caller then falls
        back to create_run). Returns the most-recently-created run when several
        exist -- TFC lists runs newest-first.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run
              (GET /workspaces/:workspace_id/runs)
        """
        started = time.monotonic()
        while True:
            response = self._request(
                "GET",
                "/workspaces/{}/runs".format(workspace_id),
                params={"page[number]": 1, "page[size]": 1},
            )
            runs = response.json().get("data", [])
            if runs:
                return runs[0]
            elapsed = time.monotonic() - started
            if elapsed + POLL_INTERVAL_SECONDS > timeout_seconds:
                return None
            if progress_callback:
                progress_callback(int(elapsed))
            time.sleep(POLL_INTERVAL_SECONDS)

    def safe_delete_workspace(self, workspace_id):
        """
        Delete a workspace via the SAFE-delete endpoint ONLY -- never
        force-delete. 404 propagates as TFCNotFoundError (teardown treats it
        as already-gone); 409 is classified into locked vs still-managing-
        resources guidance via the error detail (the documented 409 reason is
        "Workspace is not safe to delete because it is managing resources").
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces
              (POST /workspaces/:workspace_id/actions/safe-delete;
              204 deleted / 404 not found / 409 not safe to delete)
        """
        try:
            self._request("POST", "/workspaces/{}/actions/safe-delete".format(workspace_id))
        except TFCConflictError as exc:
            detail = str(exc)
            if "lock" in detail.lower():
                raise TFCConflictError(
                    "TFC refused to safe-delete workspace {}: it is locked. "
                    "Wait for (or discard) the run holding the lock, or "
                    "unlock the workspace in TFC, then retry the delete. "
                    "({})".format(workspace_id, _safe_text(detail))
                )
            raise TFCConflictError(
                "TFC refused to safe-delete workspace {}: it still manages "
                "real infrastructure. Run a destroy to completion first "
                "(deleting the CloudBolt resource does this), then retry. "
                "Never force-delete: that orphans live infrastructure. "
                "({})".format(workspace_id, _safe_text(detail))
            )
        self._log.info("Safe-deleted TFC workspace %s.", workspace_id)

    # -- variables ------------------------------------------------------------

    def list_variables(self, workspace_id):
        """
        Map of variable key -> {"id": ..., "value": ...} for a workspace.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspace-variables
              (GET /workspaces/:workspace_id/vars)
        """
        response = self._request("GET", "/workspaces/{}/vars".format(workspace_id))
        variables = {}
        for item in response.json().get("data", []):
            attributes = item.get("attributes", {}) or {}
            key = attributes.get("key")
            if key:
                variables[key] = {"id": item["id"], "value": attributes.get("value")}
        return variables

    def _variable_payload(self, attributes, variable_id=None):
        data = {"type": "vars", "attributes": attributes}
        if variable_id:
            data["id"] = variable_id
        return {"data": data}

    def upsert_variables(self, workspace_id, variables, sensitive_keys=None,
                         category="terraform"):
        """
        Upsert workspace variables: list -> PATCH by ID / POST when missing;
        a 422 on POST is treated as a lost create race -> re-list and PATCH.
        Every write explicitly pins ``category`` -- "terraform" (default) for
        the template's variables, or "env" for provider environment variables
        such as ARM_SUBSCRIPTION_ID, which the build plugins derive from the
        CloudBolt Environment. A workspace variable overrides a same-key
        variable inherited from a (non-priority) variable set.

        Value typing: a dict/list value is written ``hcl: true`` with a
        JSON-serialized expression (JSON is valid HCL2 expression syntax --
        see serialize_hcl_value, which also escapes ``${``/``%{`` to close
        the template-injection vector that previously kept this module
        hcl:false-only); any other value is written ``hcl: false`` as
        ``str(value)``.

        Sensitivity: keys named in ``sensitive_keys`` are written
        ``sensitive: true``. TFC then never returns the value again
        (list_variables reads it back as null) and the flag is ONE-WAY -- a
        PATCH attempting sensitive:false on a sensitive variable is rejected
        by TFC -- so a caller must mark such a key sensitive on EVERY write,
        or (better, and what the day-2 plugins do) leave it out of its write
        sets entirely.

        Writes exactly the keys in ``variables`` -- the caller (plugin) is
        the trust boundary and builds that dict only from its own quoted
        action inputs, so no key restriction is enforced here (keeping the
        client reusable across blueprints with different variable sets).
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspace-variables
              (POST /workspaces/:workspace_id/vars;
              PATCH /workspaces/:workspace_id/vars/:variable_id;
              category must be "terraform" or "env"; "hcl" and "sensitive"
              are per-variable booleans; hcl is ignored for env variables)
        Precedence: https://developer.hashicorp.com/terraform/cloud-docs/workspaces/variables#precedence
        """
        if category not in ("terraform", "env"):
            raise TFCError("Variable category must be 'terraform' or 'env', not '{}'.".format(category))
        sensitive = frozenset(sensitive_keys or ())
        existing = self.list_variables(workspace_id)
        for key in variables:
            native = variables[key]
            if isinstance(native, (dict, list)) and category == "terraform":
                value = serialize_hcl_value(native)
                hcl = True
            else:
                value = "" if native is None else str(native)
                hcl = False
            attributes = {
                "key": key,
                "value": value,
                "category": category,
                "hcl": hcl,
                "sensitive": key in sensitive,
            }
            def _patch_variable(variable_id, attributes=attributes):
                self._request(
                    "PATCH",
                    "/workspaces/{}/vars/{}".format(workspace_id, variable_id),
                    json_body=self._variable_payload(attributes, variable_id),
                )

            if key in existing:
                _patch_variable(existing[key]["id"])
            else:
                try:
                    self._request(
                        "POST",
                        "/workspaces/{}/vars".format(workspace_id),
                        json_body=self._variable_payload(attributes),
                    )
                except TFCValidationError:
                    # Lost race: the key appeared between list and POST.
                    refreshed = self.list_variables(workspace_id)
                    if key not in refreshed:
                        raise
                    _patch_variable(refreshed[key]["id"])
            self._log.info("Upserted TFC workspace variable '%s'.", key)

    # -- configuration versions ------------------------------------------------

    def wait_for_config_version(self, workspace_id,
                                timeout_seconds=CONFIG_VERSION_TIMEOUT_SECONDS,
                                progress_callback=None,
                                repo_identifier=None, branch=None):
        """
        Wait (bounded) for the workspace's VCS configuration version to be
        ingested. Returns the usable configuration-version ID. Per the docs,
        only ``uploaded`` versions can be used in runs (``archived``
        VCS-backed versions are re-fetchable and also usable); a newest
        version stuck on ``errored`` fails fast with branch/repo guidance.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/configuration-versions
              (GET /workspaces/:workspace_id/configuration-versions; status
              enum pending/fetching/uploaded/archived/errored)
        """
        started = time.monotonic()
        while True:
            response = self._request(
                "GET", "/workspaces/{}/configuration-versions".format(workspace_id)
            )
            versions = response.json().get("data", [])
            statuses = [
                (version.get("attributes", {}) or {}).get("status")
                for version in versions
            ]
            for version, status in zip(versions, statuses):
                if status in ("uploaded", "archived"):
                    return version["id"]
            if statuses and all(status == "errored" for status in statuses):
                # Every ingestion attempt failed -- polling cannot fix this.
                repo_hint = "repo '{}', branch '{}'".format(
                    repo_identifier, branch
                ) if repo_identifier else "the repo/branch pinned on the blueprint"
                raise TFCError(
                    "TFC could not ingest the Terraform configuration for "
                    "workspace {} (configuration version errored). Check {} "
                    "and the VCS connection in TFC.".format(workspace_id, repo_hint)
                )
            latest_status = statuses[0] if statuses else None
            elapsed = time.monotonic() - started
            if progress_callback is not None:
                progress_callback(latest_status or "awaiting first version", elapsed)
            if elapsed + POLL_INTERVAL_SECONDS > timeout_seconds:
                repo_hint = "'{}' (branch '{}')".format(
                    repo_identifier, branch
                ) if repo_identifier else "the repo/branch pinned on the blueprint"
                raise TFCTimeoutError(
                    "Timed out after {}s waiting for workspace {} to ingest a "
                    "configuration version from {} (last status: {}). Verify "
                    "the repo/branch and the VCS OAuth connection in "
                    "TFC.".format(
                        int(elapsed), workspace_id, repo_hint, latest_status,
                    )
                )
            time.sleep(POLL_INTERVAL_SECONDS)

    # -- runs -----------------------------------------------------------------

    def create_run(self, workspace_id, message, is_destroy=False):
        """
        Create a run. ``message`` must come from build_run_message() so it
        carries only non-PII identifiers and teardown can parse the owning
        job ID back out.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run
              (POST /runs with data.attributes.{message, is-destroy} and
              data.relationships.workspace.data)
        """
        payload = {
            "data": {
                "type": "runs",
                "attributes": {
                    "message": message,
                    "is-destroy": bool(is_destroy),
                },
                "relationships": {
                    "workspace": {
                        "data": {"type": "workspaces", "id": workspace_id},
                    },
                },
            }
        }
        response = self._request("POST", "/runs", json_body=payload)
        run = response.json()["data"]
        self._log.info(
            "Created TFC %s run %s on workspace %s.",
            "destroy" if is_destroy else "plan/apply", run.get("id"), workspace_id,
        )
        return run

    def get_run(self, run_id):
        """
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run
              (GET /runs/:run_id; attributes.status + attributes.actions)
        """
        response = self._request("GET", "/runs/{}".format(run_id))
        return response.json()["data"]

    def run_app_url(self, run_id, workspace_name):
        """
        Human-facing deep link to a run in the TFC UI. Shape confirmed from
        HashiCorp's documented notification payload sample run_url
        ("https://app.terraform.io/app/acme-org/my-workspace/runs/run-...").
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/notification-configurations/workspace
        """
        return "https://{}/app/{}/{}/runs/{}".format(
            self.host, self._require_org(), workspace_name, run_id
        )

    def poll_run(self, run_id, timeout_seconds, progress_callback=None):
        """
        Poll a run (bounded) until it leaves the in-flight class. Returns
        ``(classification, run_data)`` where classification is one of the
        RUN_CLASS_* constants other than RUN_CLASS_IN_FLIGHT. 429 backoff is
        handled inside _request; unknown statuses keep polling toward the
        bounded timeout (warned on each poll).
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run
              https://developer.hashicorp.com/terraform/cloud-docs/run/states
        """
        started = time.monotonic()
        while True:
            run = self.get_run(run_id)
            classification = classify_run(run)
            if classification != RUN_CLASS_IN_FLIGHT:
                return classification, run
            status = (run.get("attributes", {}) or {}).get("status", "unknown")
            if status not in KNOWN_IN_FLIGHT_STATUSES:
                self._log.warning(
                    "TFC run %s reports unrecognized status '%s'; continuing "
                    "to poll (bounded).", run_id, status,
                )
            elapsed = time.monotonic() - started
            if progress_callback is not None:
                progress_callback(status, elapsed)
            if elapsed + POLL_INTERVAL_SECONDS > timeout_seconds:
                raise TFCTimeoutError(
                    "Timed out after {}s waiting for TFC run {} (last status "
                    "'{}'). The run may still be progressing in TFC -- check "
                    "it there before retrying.".format(int(elapsed), run_id, status)
                )
            time.sleep(POLL_INTERVAL_SECONDS)

    def apply_run(self, run_id, comment=None):
        """
        Confirm-and-apply a run. A 409 (TFCConflictError) means the run was
        not awaiting confirmation -- callers must re-read the run state and
        re-branch rather than fail blindly.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run
              (POST /runs/:run_id/actions/apply; 202 queued / 409 "Run was
              not paused for confirmation; apply not allowed")
        """
        payload = {"comment": comment} if comment else None
        self._request("POST", "/runs/{}/actions/apply".format(run_id), json_body=payload)
        self._log.info("Requested apply of TFC run %s.", run_id)

    def discard_run(self, run_id, comment=None):
        """
        Discard a run awaiting confirmation. A 409 (TFCConflictError) means
        the run is not discardable (already applied/discarded, or actively
        applying) -- callers decide whether that is benign.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run
              (POST /runs/:run_id/actions/discard; 202 / 409 "Run was not
              paused for confirmation or priority; discard not allowed")
        """
        payload = {"comment": comment} if comment else None
        self._request("POST", "/runs/{}/actions/discard".format(run_id), json_body=payload)
        self._log.info("Discarded TFC run %s.", run_id)

    def list_non_final_runs(self, workspace_id):
        """
        Runs on the workspace that have not reached a terminal state --
        running, awaiting confirmation, or queued. Returns enough for
        teardown's attribution-aware discard: id, status, and message
        (carrying the owning CloudBolt job ID).
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run
              (GET /workspaces/:workspace_id/runs with
              filter[status_group]=non_final; status_group accepts
              non_final, final, discardable)
        """
        page_size = 100
        response = self._request(
            "GET",
            "/workspaces/{}/runs".format(workspace_id),
            params={"filter[status_group]": "non_final", "page[size]": page_size},
        )
        data = response.json().get("data", [])
        # Teardown's discard-all-orphans guarantee and the day-2 fail-fast guard
        # both assume this list is COMPLETE. In the one-workspace-per-deployment
        # design (CloudBolt creates runs explicitly and day-2 fails fast on any
        # pending run) a full page of non-final runs should never happen, so we
        # fail loudly rather than silently truncate and act on a partial view.
        if len(data) >= page_size:
            raise TFCError(
                "Workspace {} reports a full page ({}) of non-final runs -- "
                "an unexpected backlog this integration does not paginate. "
                "Inspect the workspace's runs in TFC before retrying.".format(
                    workspace_id, page_size
                )
            )
        runs = []
        for item in data:
            attributes = item.get("attributes", {}) or {}
            runs.append({
                "id": item["id"],
                "status": attributes.get("status"),
                "message": attributes.get("message") or "",
            })
        return runs

    # -- plans ----------------------------------------------------------------

    def get_plan_summary(self, run_id, include_log=True):
        """
        Plan summary for a run: resource add/change/destroy COUNTS first,
        then (optionally) a tail-preserving, secret-stripped excerpt of the
        human-readable plan log. The pre-signed ``log-read-url`` is consumed
        internally and never returned -- it grants unauthenticated log access
        for its validity window, so it must never reach job output.

        The structured json-output endpoint is deliberately not used: it
        requires workspace-admin token access and answers with a redirect
        URL valid for one minute.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/plans
              (GET /plans/:id; attributes resource-additions,
              resource-changes, resource-destructions, status, log-read-url)
        """
        run = self.get_run(run_id)
        plan_ref = ((run.get("relationships", {}) or {}).get("plan", {}) or {}).get("data") or {}
        plan_id = plan_ref.get("id")
        summary = {
            "additions": None,
            "changes": None,
            "destructions": None,
            "plan_status": None,
            "log_excerpt": "",
            "warnings": "",
        }
        if not plan_id:
            self._log.warning("TFC run %s has no plan relationship yet.", run_id)
            return summary
        response = self._request("GET", "/plans/{}".format(plan_id))
        attributes = response.json()["data"].get("attributes", {}) or {}
        summary["additions"] = attributes.get("resource-additions")
        summary["changes"] = attributes.get("resource-changes")
        summary["destructions"] = attributes.get("resource-destructions")
        summary["plan_status"] = attributes.get("status")
        if include_log:
            log_read_url = attributes.get("log-read-url")
            if log_read_url:
                try:
                    raw_log = self._fetch_plan_log(log_read_url)
                    # R3: surface terraform's own "Warning:" lines (e.g. "Value
                    # for undeclared variable" from a mistyped variable name
                    # whose real target still has a .tf default, so the plan
                    # SUCCEEDS silently on the stale default). Scanned from the
                    # FULL log BEFORE the tail excerpt so a warning above the
                    # tail cap is not dropped; redacted with the SAME secret
                    # patterns as the excerpt before it can reach job output.
                    summary["warnings"] = _redact_plan_log(_extract_plan_warnings(raw_log))
                    # Truncate first, then redact: redaction is line-local and
                    # 1:1, so only the surviving tail needs the regex scan.
                    summary["log_excerpt"] = _redact_plan_log(_tail_excerpt(raw_log))
                except TFCError as exc:
                    # Log availability must not block the approval gate; the
                    # counts and run URL still give the approver the gist.
                    self._log.warning("Plan log unavailable for run %s: %s", run_id, exc)
                    summary["log_excerpt"] = (
                        "(plan log unavailable: {} -- review the full log at "
                        "the TFC run URL)".format(_safe_text(exc))
                    )
        return summary

    def _fetch_plan_log(self, log_read_url):
        """
        Fetch the human-readable plan log from its pre-signed URL. The fetch
        is TOKENLESS (plain requests, not the authorized session -- the URL
        embeds its own grant and the bearer token must not leak to the log
        store). Each URL must be https with a host on
        PLAN_LOG_HOST_ALLOWLIST; redirects are NOT auto-followed and every
        redirect Location is re-validated against the same allowlist.
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/plans
              (log-read-url sample: https://archivist.terraform.io/v1/object/...)
        """
        current_url = log_read_url
        for _hop in range(PLAN_LOG_MAX_REDIRECTS + 1):
            _validate_log_url(current_url)
            try:
                response = requests.get(
                    current_url,
                    allow_redirects=False,
                    timeout=HTTP_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                # NEVER interpolate the exception text: a requests error embeds
                # the request URL, and current_url is the pre-signed log-read-url
                # whose path carries an unauthenticated access grant. Surface
                # only the exception class name so the grant never reaches the
                # raised message, the warning log, or the group-visible excerpt.
                raise TFCError(
                    "Plan log fetch failed ({}).".format(type(exc).__name__)
                )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise TFCError("Plan log fetch redirected without a Location header.")
                current_url = urljoin(current_url, location)
                continue
            if response.status_code != 200:
                raise TFCError(
                    "Plan log fetch returned HTTP {}.".format(response.status_code)
                )
            return response.text
        raise TFCError(
            "Plan log fetch exceeded {} redirects.".format(PLAN_LOG_MAX_REDIRECTS)
        )

    # -- state outputs ----------------------------------------------------------

    def wait_for_outputs(self, workspace_id, output_names=None,
                         timeout_seconds=OUTPUTS_TIMEOUT_SECONDS):
        """
        Current state-version outputs for a workspace. By default (
        ``output_names=None``) EVERY output in the state is returned --
        CloudBolt discovers the template's outputs rather than requiring them
        to be declared; pass an explicit list to filter. Returns
        ``{name: value}`` (possibly empty), or ``None`` when the workspace has
        NO current state version at all (nothing ever applied) -- callers use
        that to skip hydration.

        The endpoint answers 503 while the platform is still parsing the new
        state ("State version outputs are being processed... Retry the
        request"); this retries, bounded, until 200 / timeout -- i.e. until
        the state version's ``resources-processed`` work completes. Outputs
        marked sensitive come back ``null`` on this endpoint regardless
        ("Sensitive values are not revealed and will be returned as null").
        Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/state-version-outputs
              (GET /workspaces/:workspace_id/current-state-version-outputs)
        """
        allowed = set(output_names) if output_names is not None else None
        path = "/workspaces/{}/current-state-version-outputs".format(workspace_id)
        started = time.monotonic()
        while True:
            try:
                response = self._request(
                    "GET",
                    path,
                    params={"page[number]": 1, "page[size]": LIST_PAGE_SIZE},
                    allowed_statuses=(503,),
                )
            except TFCNotFoundError:
                self._log.info(
                    "Workspace %s has no current state version; no outputs to "
                    "read.", workspace_id,
                )
                return None
            if response.status_code == 503:
                elapsed = time.monotonic() - started
                if elapsed + POLL_INTERVAL_SECONDS > timeout_seconds:
                    raise TFCTimeoutError(
                        "Timed out after {}s waiting for TFC to process state "
                        "outputs for workspace {}.".format(int(elapsed), workspace_id)
                    )
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            # 200: the outputs are processed. The endpoint is paginated, and
            # since output discovery is automatic (no declared allowlist), a
            # template with many outputs must not be silently truncated at the
            # first page -- collect the remaining pages (bounded).
            items = list(response.json().get("data", []))
            pagination = (
                (response.json().get("meta", {}) or {}).get("pagination", {}) or {}
            )
            next_page = pagination.get("next-page")
            pages_fetched = 1
            while next_page and pages_fetched < LIST_MAX_PAGES:
                page_response = self._request(
                    "GET",
                    path,
                    params={"page[number]": next_page, "page[size]": LIST_PAGE_SIZE},
                )
                body = page_response.json()
                items.extend(body.get("data", []))
                next_page = (
                    (body.get("meta", {}) or {}).get("pagination", {}) or {}
                ).get("next-page")
                pages_fetched += 1
            outputs = {}
            for item in items:
                attributes = item.get("attributes", {}) or {}
                name = attributes.get("name")
                if name and (allowed is None or name in allowed):
                    outputs[name] = attributes.get("value")
            return outputs


# =============================================================================
# == CloudBolt seam -- the ONLY code below may touch CloudBolt objects ========
# =============================================================================

def resolve_connection_info(connection_info_ref=None):
    """
    Resolve the 'tf-cloud'-labeled (CONNECTION_INFO_LABEL) ConnectionInfo
    carrying the TFC team token.

    ``connection_info_ref`` is the order form's dropdown value (the
    ConnectionInfo global_id) or, tolerantly, its name -- the build plugin
    stores it on the resource so day-2/teardown resolve the SAME connection.
    The label is ENFORCED even when a ref is supplied: the ref is a plain
    form value an orderer could tamper with, and the label is what an admin
    uses to mark a ConnectionInfo as safe for this integration.

    With no ref (legacy resources provisioned before connection selection
    existed), exactly one labeled ConnectionInfo resolves unambiguously;
    zero or several raise an operator-actionable TFCConfigError.
    """
    labeled = ConnectionInfo.objects.filter(labels__name=CONNECTION_INFO_LABEL)
    reference = (connection_info_ref or "").strip()
    if reference:
        connection_info = (
            labeled.filter(global_id=reference).first()
            or labeled.filter(name=reference).first()
        )
        if connection_info is None:
            raise TFCConfigError(
                "No ConnectionInfo labeled '{}' matches '{}'. Confirm the "
                "ConnectionInfo still exists and carries the '{}' label "
                "(Admin > Connection Info), then re-select it on the order "
                "form (or update the resource's tfc_connection_info "
                "value).".format(
                    CONNECTION_INFO_LABEL, reference, CONNECTION_INFO_LABEL
                )
            )
        return connection_info
    count = labeled.count()
    if count == 1:
        return labeled.first()
    if count == 0:
        raise TFCConfigError(
            "No ConnectionInfo on this CloudBolt instance carries the '{}' "
            "label. Create one (Admin > Connection Info) with protocol https, "
            "ip {} (or your TFE host), port 443, an HCP Terraform TEAM token "
            "in the password field, and the '{}' label. See "
            "docs/hcp-terraform-setup.md.".format(
                CONNECTION_INFO_LABEL, TFC_DEFAULT_HOST, CONNECTION_INFO_LABEL
            )
        )
    raise TFCConfigError(
        "{} ConnectionInfos carry the '{}' label, and no specific connection "
        "was recorded for this operation. Set the resource's "
        "tfc_connection_info field to the intended ConnectionInfo global_id, "
        "or keep a single labeled ConnectionInfo.".format(
            count, CONNECTION_INFO_LABEL
        )
    )


def _client_from_connection_info(connection_info, organization=None):
    """Build a TFCClient from a resolved ConnectionInfo (host from its ip
    field, token from its password field)."""
    token = (connection_info.password or "").strip()
    if not token:
        raise TFCConfigError(
            "ConnectionInfo '{}' has no token in its password field. Repo "
            "syncs redact secrets, so the HCP Terraform team token must be "
            "re-entered in the CloudBolt UI after every sync.".format(
                connection_info.name
            )
        )
    host = (connection_info.ip or "").strip() or TFC_DEFAULT_HOST
    if "://" in host:
        # Tolerate an operator pasting a full URL into the ip field.
        host = urlsplit(host).netloc or TFC_DEFAULT_HOST
    return TFCClient(host=host, token=token, organization=organization, log=logger)


def get_client(organization, connection_info_ref=None):
    """
    Build a TFCClient for ``organization`` (selected on the order form and
    stored on the resource) from the 'tf-cloud'-labeled ConnectionInfo
    resolved via ``connection_info_ref`` (see resolve_connection_info).
    Missing or incomplete ConnectionInfo, a blank organization, and unfilled
    account-level FILL-ME config all raise operator-actionable TFCConfigError.
    """
    if not (organization or "").strip():
        raise TFCConfigError(
            "No TFC organization was provided. It is selected on the order "
            "form and stored on the resource for day-2/teardown -- see "
            "docs/hcp-terraform-setup.md."
        )
    connection_info = resolve_connection_info(connection_info_ref)
    client = _client_from_connection_info(connection_info, organization=organization)
    set_progress(
        "Connecting to HCP Terraform at {} using ConnectionInfo '{}'.".format(
            client.host, connection_info.name
        )
    )
    return client


def get_options_client(connection_info_ref, organization=None):
    """
    Organization-optional TFCClient for the order form's
    ``generate_options_for_*`` lookups (organizations / projects / repos).
    No set_progress noise -- this runs at form-render time, not in a job.
    """
    connection_info = resolve_connection_info(connection_info_ref)
    return _client_from_connection_info(connection_info, organization=organization)


# Terraform output names become tfc_output_<name> custom fields. HCL
# identifiers are letters/digits/underscores/hyphens; anything else is not a
# real terraform output name and is skipped (defensively) rather than turned
# into a malformed CustomField name.
# Docs: https://developer.hashicorp.com/terraform/language/syntax/configuration#identifiers
OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")


def ensure_output_custom_fields(output_names):
    """
    Create a ``tfc_output_<name>`` CustomField for every DISCOVERED state
    output (idempotent), returning the names that are valid HCL identifiers
    (invalid ones are skipped with a warning). Shared by the build and day-2
    plugins: the output set is parsed from the applied Terraform state, never
    declared, so the fields must be creatable on the fly. This helper defines
    CustomFields only -- setting VALUES on a resource stays in the plugins.
    """
    valid_names = []
    for output_name in output_names:
        if not OUTPUT_NAME_RE.match(output_name or ""):
            logger.warning(
                "Skipping Terraform output %r: not a valid identifier for a "
                "tfc_output_* custom field.", output_name,
            )
            continue
        CustomField.objects.get_or_create(
            name="tfc_output_{}".format(output_name),
            defaults=dict(
                label="TFC Output: {}".format(output_name),
                description="Value of the '{}' Terraform state output for "
                            "this deployment.".format(output_name),
                type="STR",
                show_on_servers=False,
            ),
        )
        valid_names.append(output_name)
    return valid_names


# Progress-bar model for one engine invocation:
# 1 run created -> 2 plan polled -> 3 decision -> 4 apply polled -> 5 terminal.
TOTAL_ENGINE_TASKS = 5


def run_with_plan_approval(job, client, workspace_id, message,
                           auto_confirm=False, is_destroy=False, on_reject=None):
    """
    Create a TFC run on ``workspace_id`` and drive it to a terminal state
    with a human plan-approval gate.

    Flow: create run -> poll (classification per classify_run, gated on
    actions.is-confirmable) -> on confirmable, write the plan summary
    (counts first, then a tail-preserving secret-stripped log excerpt, plus
    the TFC run URL) to job output and ``job.pause()`` -- unless
    ``auto_confirm`` (teardown passes ``auto_confirm=True, is_destroy=True``)
    -> on resume, RE-READ the run state and re-branch (still confirmable ->
    apply, 409 -> re-read again; applied out-of-band in TFC -> log and
    continue; discarded in TFC -> failure with run URL) -> poll to terminal.

    Returns ``{"status": "applied" | "planned_and_finished",
    "run_id": ..., "run_url": ...}`` (plus plan counts when fetched);
    ``planned_and_finished`` is success-with-no-changes everywhere. Raises
    TFCRunFailedError (run URL attached) on errored/discarded/canceled/
    force_canceled, and on ``policy_override`` (out of POC scope) with
    guidance to resolve the policy in the TFC UI.

    Reject ownership: this function -- never the calling plugin -- catches
    ``CancelJobException`` (BaseException subclass; ``except Exception`` does
    NOT see it). It discards the run and calls ``on_reject()`` (day-2 plugins
    revert workspace variables there) ONLY while the run is still pre-apply; if
    the apply has already begun, the apply completes in TFC regardless, so the
    run is left alone and NOT reverted (see ``_handle_reject``). It then
    re-raises so the cancellation completes.

    Error ownership: an unexpected ``TFCError``/timeout from the run lifecycle
    (transient 5xx, a bounded-poll timeout, a plan-summary fetch failure) is
    caught too, the non-final run is best-effort discarded so it cannot orphan
    the workspace, and the error is re-raised. A classified terminal failure
    (``TFCRunFailedError`` -- errored/abandoned/policy_override) is re-raised
    untouched. This is the only surface that touches ``job``; it never touches
    the resource.
    """
    workspace = client.get_workspace(workspace_id)
    workspace_name = (workspace.get("attributes", {}) or {}).get("name", workspace_id)

    run = client.create_run(workspace_id, message, is_destroy=is_destroy)
    run_id = run["id"]
    run_url = client.run_app_url(run_id, workspace_name)
    job.set_progress(
        "Created TFC {} run {} on workspace '{}'.".format(
            "destroy" if is_destroy else "plan", run_id, workspace_name
        ),
        tasks_done=1, total_tasks=TOTAL_ENGINE_TASKS,
    )

    return _drive_run_with_ownership(
        job, client, run_id, run_url, auto_confirm=auto_confirm, on_reject=on_reject
    )


def drive_run_with_plan_approval(job, client, workspace_id, run_id, on_reject=None):
    """Adopt an EXISTING run and drive it through the same plan-approval gate as
    run_with_plan_approval -- for the no-code build path, where
    POST /no-code-modules/:id/workspaces AUTO-QUEUES the run rather than letting
    CloudBolt create it (run_with_plan_approval creates its own run; this adopts
    one the plugin already located via wait_for_initial_run / list_non_final_runs).

    Same ownership guarantees as run_with_plan_approval: this function -- never
    the calling plugin -- catches CancelJobException, discards + reverts while
    pre-apply, and re-raises. No ``auto_confirm``/``is_destroy``: an adopted
    no-code run is the provision run and always pauses for approval (teardown
    still uses run_with_plan_approval with auto_confirm=True, is_destroy=True).

    Attribution: the auto-queued run's message is TFC-authored ("Triggered via
    no-code provision"), so it is NOT parseable by parse_job_id_from_run_message;
    the caller stores ``run_id`` on the resource as the attribution source.
    """
    workspace = client.get_workspace(workspace_id)
    workspace_name = (workspace.get("attributes", {}) or {}).get("name", workspace_id)
    run_url = client.run_app_url(run_id, workspace_name)
    job.set_progress(
        "Adopting TFC run {} on workspace '{}'.".format(run_id, workspace_name),
        tasks_done=1, total_tasks=TOTAL_ENGINE_TASKS,
    )
    return _drive_run_with_ownership(
        job, client, run_id, run_url, auto_confirm=False, on_reject=on_reject
    )


def _drive_run_with_ownership(job, client, run_id, run_url, auto_confirm, on_reject):
    """Shared reject/error-ownership wrapper around _drive_run, called by both
    run_with_plan_approval (CREATES the run) and drive_run_with_plan_approval
    (ADOPTS an existing run). Behavior is identical for both entry points.
    """
    try:
        return _drive_run(job, client, run_id, run_url, auto_confirm=auto_confirm)
    except CancelJobException:
        # Reject path (user cancel or global job_timeout). Discard + revert
        # only while the run is still pre-apply; if the apply already started,
        # cancellation cannot undo it, so reverting would diverge the workspace
        # from what TFC is applying. _handle_reject decides; we always re-raise.
        _handle_reject(job, client, run_id, run_url, on_reject)
        raise
    except TFCRunFailedError:
        # The run reached a classified terminal/awaiting state (errored,
        # discarded, or policy_override). It is already final, or -- for
        # policy_override -- intentionally left for the operator to resolve in
        # the TFC UI per the plan. Do not discard; the message carries the URL.
        raise
    except TFCError:
        # An UNEXPECTED failure (transient 5xx, a bounded-poll timeout, or a
        # plan-summary fetch error) left the run non-final with no terminal
        # classification. Best-effort discard so it does not orphan the
        # workspace and block every future run (day-2 fail-fast + provision
        # retry). Tolerates 409 if the run is actually applying/terminal.
        _best_effort_discard(job, client, run_id)
        job.set_progress(
            "TFC run {} did not complete; discarded it if it was still "
            "pending so it will not block future runs. Review {}".format(
                run_id, run_url
            ),
            tasks_done=TOTAL_ENGINE_TASKS, total_tasks=TOTAL_ENGINE_TASKS,
        )
        raise


def _run_apply_started(client, run_id):
    """True if the run's apply is confirmed/underway/done (point of no return).

    Returns False when the run is still pre-apply (confirmable/planning) AND
    when its state cannot be read -- the caller treats unknown as pre-apply so
    the common reject-during-pause path (discard + revert) still runs.
    """
    try:
        run = client.get_run(run_id)
    except TFCError as exc:
        logger.warning("Could not re-read TFC run %s on reject: %s", run_id, exc)
        return False
    status = (run.get("attributes", {}) or {}).get("status", "")
    return status in APPLY_STARTED_STATUSES


def _best_effort_discard(job, client, run_id):
    """Discard a run, tolerating the run already being terminal/applying (409).

    Used on both the reject path and the unexpected-error path so a run this
    engine created never orphans the workspace. Never raises -- it must not
    mask the exception that triggered it.
    """
    try:
        client.discard_run(
            run_id, comment="Discarded by CloudBolt job {}.".format(job.id)
        )
    except TFCError as exc:
        logger.warning("Could not discard TFC run %s: %s", run_id, exc)


def _handle_reject(job, client, run_id, run_url, on_reject):
    """Reject-path cleanup: discard the run and run the caller's revert, but
    ONLY while the run is still pre-apply. If the apply already started, the
    apply will complete in TFC regardless of the cancel, so discarding (409)
    and reverting workspace variables would manufacture infra/variable/mirror
    divergence -- instead we report honestly and leave the run and variables
    alone. Never raises; the caller re-raises CancelJobException.
    """
    if _run_apply_started(client, run_id):
        job.set_progress(
            "Cancellation received, but TFC run {} has already begun applying "
            "-- the apply will complete in TFC and is NOT being reverted. "
            "Review the result at {}".format(run_id, run_url),
            tasks_done=TOTAL_ENGINE_TASKS, total_tasks=TOTAL_ENGINE_TASKS,
        )
        return
    job.set_progress(
        "Cancellation received -- discarding TFC run {} (run URL: {}).".format(
            run_id, run_url
        ),
        tasks_done=TOTAL_ENGINE_TASKS, total_tasks=TOTAL_ENGINE_TASKS,
    )
    _best_effort_discard(job, client, run_id)
    if on_reject is not None:
        try:
            on_reject()
        except Exception:
            # Caller cleanup must not mask the cancellation either.
            logger.exception("on_reject callback failed during job cancellation.")


def _poll_progress_callback(job, run_id, tasks_done, phase_label):
    """set_progress wrapper for poll loops -- always carries the task bar."""
    def _callback(status, elapsed_seconds):
        job.set_progress(
            "TFC run {} {}: status '{}' ({}s elapsed).".format(
                run_id, phase_label, status, int(elapsed_seconds)
            ),
            tasks_done=tasks_done, total_tasks=TOTAL_ENGINE_TASKS,
        )
    return _callback


def _terminal_result(job, classification, run_id, run_url, summary=None):
    """Translate a terminal classification into the engine's return/raise."""
    if classification == RUN_CLASS_APPLIED:
        job.set_progress(
            "TFC run {} applied successfully.".format(run_id),
            tasks_done=TOTAL_ENGINE_TASKS, total_tasks=TOTAL_ENGINE_TASKS,
        )
        result = {"status": RUN_CLASS_APPLIED, "run_id": run_id, "run_url": run_url}
    elif classification == RUN_CLASS_NO_CHANGES:
        job.set_progress(
            "TFC run {} finished with no changes to apply "
            "(planned_and_finished).".format(run_id),
            tasks_done=TOTAL_ENGINE_TASKS, total_tasks=TOTAL_ENGINE_TASKS,
        )
        result = {"status": RUN_CLASS_NO_CHANGES, "run_id": run_id, "run_url": run_url}
    elif classification == RUN_CLASS_ABANDONED:
        raise TFCRunFailedError(
            "TFC run {} was discarded/canceled before completion. Review it "
            "at {}".format(run_id, run_url),
            run_url=run_url,
        )
    elif classification == RUN_CLASS_POLICY_OVERRIDE:
        raise TFCRunFailedError(
            "TFC run {} requires a policy override, which is out of scope "
            "for this integration. Resolve the policy check in the TFC UI "
            "({}), then retry the action.".format(run_id, run_url),
            run_url=run_url,
        )
    elif classification == RUN_CLASS_ERRORED:
        raise TFCRunFailedError(
            "TFC run {} errored. Review the run log at {}".format(run_id, run_url),
            run_url=run_url,
        )
    else:
        raise TFCError(
            "TFC run {} reached unexpected state '{}' ({}).".format(
                run_id, classification, run_url
            )
        )
    if summary:
        for key in ("additions", "changes", "destructions"):
            if summary.get(key) is not None:
                result[key] = summary[key]
    return result


def _drive_run(job, client, run_id, run_url, auto_confirm):
    """Plan-poll, decide (pause or auto-confirm), apply, poll to terminal."""
    classification, _run = client.poll_run(
        run_id,
        timeout_seconds=PLAN_PHASE_TIMEOUT_SECONDS,
        progress_callback=_poll_progress_callback(job, run_id, 2, "planning"),
    )

    if classification != RUN_CLASS_CONFIRMABLE:
        # Terminal straight out of the plan phase (no-op plan, plan error,
        # canceled in TFC, policy override...).
        return _terminal_result(job, classification, run_id, run_url)

    # --- decision point: the run awaits confirmation -------------------------
    summary = client.get_plan_summary(run_id, include_log=not auto_confirm)
    counts_line = (
        "Terraform plan summary: {} to add, {} to change, {} to destroy.".format(
            summary["additions"], summary["changes"], summary["destructions"]
        )
    )
    job.set_progress(counts_line, tasks_done=3, total_tasks=TOTAL_ENGINE_TASKS)
    job.set_progress(
        "TFC run URL: {}".format(run_url),
        tasks_done=3, total_tasks=TOTAL_ENGINE_TASKS,
    )

    if auto_confirm:
        job.set_progress(
            "Auto-confirming TFC run {} (no approval pause requested).".format(run_id),
            tasks_done=3, total_tasks=TOTAL_ENGINE_TASKS,
        )
    else:
        if summary["warnings"]:
            # R3: an undeclared-variable typo whose real target has a .tf
            # default plans SUCCESSFULLY on the stale default -- terraform only
            # WARNS. Emit those warnings as a distinct, clearly-labeled block
            # ABOVE the tail excerpt so the approver cannot miss them. This is a
            # separate set_progress from the tail excerpt and is built from the
            # FULL log (see get_plan_summary), so the tail cap never truncates
            # it away.
            job.set_progress(
                "⚠ Terraform warnings (review before approving this plan):\n"
                "{}".format(summary["warnings"]),
                tasks_done=3, total_tasks=TOTAL_ENGINE_TASKS,
            )
        if summary["log_excerpt"]:
            job.set_progress(
                "Terraform plan log (tail):\n{}".format(summary["log_excerpt"]),
                tasks_done=3, total_tasks=TOTAL_ENGINE_TASKS,
            )
        job.set_progress(
            "PAUSING for plan approval. 'Continue Job' approves and applies "
            "this plan; canceling the job rejects it and discards TFC run "
            "{}.".format(run_id),
            tasks_done=3, total_tasks=TOTAL_ENGINE_TASKS,
        )
        # Parks this job thread until a human continues (approve) or cancels
        # (reject -- raises CancelJobException, handled by the caller).
        job.pause()
        job.set_progress(
            "Job resumed -- re-reading TFC run {} before applying.".format(run_id),
            tasks_done=3, total_tasks=TOTAL_ENGINE_TASKS,
        )

    _confirm_run(job, client, run_id, run_url)

    # --- apply phase ----------------------------------------------------------
    classification, _run = client.poll_run(
        run_id,
        timeout_seconds=APPLY_PHASE_TIMEOUT_SECONDS,
        progress_callback=_poll_progress_callback(job, run_id, 4, "applying"),
    )
    if classification == RUN_CLASS_CONFIRMABLE:
        # A run cannot lawfully return to confirmable after an apply was
        # accepted; treat defensively rather than loop forever.
        raise TFCError(
            "TFC run {} unexpectedly returned to a confirmable state after "
            "apply ({}).".format(run_id, run_url)
        )
    return _terminal_result(job, classification, run_id, run_url, summary=summary)


def _confirm_run(job, client, run_id, run_url):
    """
    POST the apply, re-read-and-re-branch style: the run state is re-read
    first (it may have changed while the job was paused), a 409 on apply
    triggers another re-read, an out-of-band apply via the TFC UI is logged
    and accepted, and an out-of-band discard fails with the run URL.
    """
    while True:
        run = client.get_run(run_id)
        classification = classify_run(run)
        if classification == RUN_CLASS_CONFIRMABLE:
            try:
                client.apply_run(
                    run_id, comment="Approved via CloudBolt job {}.".format(job.id)
                )
            except TFCConflictError:
                # 409: the run moved between our read and the apply.
                logger.info(
                    "Apply of TFC run %s returned 409; re-reading run state.", run_id
                )
                continue
            job.set_progress(
                "Apply of TFC run {} confirmed.".format(run_id),
                tasks_done=4, total_tasks=TOTAL_ENGINE_TASKS,
            )
            return
        if classification in (RUN_CLASS_IN_FLIGHT, RUN_CLASS_APPLIED):
            # Past the confirmation gate without our apply: someone with TFC
            # access confirmed it directly. Accept and record.
            job.set_progress(
                "TFC run {} was approved out-of-band (applied directly in "
                "TFC); continuing.".format(run_id),
                tasks_done=4, total_tasks=TOTAL_ENGINE_TASKS,
            )
            return
        if classification == RUN_CLASS_NO_CHANGES:
            # Nothing to confirm; the terminal poll will return it.
            return
        if classification == RUN_CLASS_ABANDONED:
            raise TFCRunFailedError(
                "TFC run {} was discarded in the TFC UI while CloudBolt was "
                "awaiting approval. Review it at {}".format(run_id, run_url),
                run_url=run_url,
            )
        # errored / policy_override / anything else terminal.
        _terminal_result(job, classification, run_id, run_url)
        return
