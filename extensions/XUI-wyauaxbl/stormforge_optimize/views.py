"""
StormForge Optimize Live XUI

Adds three tabs to every Kubernetes cluster resource that has the StormForge
agent: Clusters, Namespaces and Workloads. Each tab shows the StormForge icon
in place of the word StormForge. The tabs appear only on resources that carry
both stormforge_cluster_name and stormforge_agent_version, which the StormForge
agent install (shared module SHM-2fd3bhsz, used by the AWS EKS, Azure
Kubernetes Service and Google Kubernetes Engine blueprints) writes on the
cluster once Helm has installed the agent.

- Clusters: the optimization score, recommendation summary and impact cards,
  then Namespaces (StormForge's paginated, searchable, sortable namespace
  table), Nodes (a live snapshot of the cluster's nodes: CPU, memory and pod
  used, requested and capacity, node count by type, and every node) and
  Config (cluster labels, node instance categories and cost estimates).
- Namespaces: a namespace's cards and its workload table. Without a
  namespace chosen, the namespace table.
- Workloads: a workload's score, potential savings and recommendation status,
  then Summary, Recommendation, Patches and Settings (Goals, Schedule, Apply
  Settings, Reliability, CPU and Memory, HPA, Runtime, Pod Scheduling, each
  with StormForge's annotation examples). Without a workload chosen, the
  cluster's workload table with a namespace filter.

Clicking a namespace or workload anywhere switches to its tab with it chosen.
The cluster tabs are read-only: StormForge settings are applied as
annotations in Kubernetes, so the tabs show them with the annotations that
change them.

Namespace resources (AWS EKS Namespace, AKS Namespace, GKE Namespace) on such
a cluster get one tab, Optimization, when this CloudBolt has the StormForge
connection info. It shows that namespace only: its cards and workload table,
and each workload's Summary, Recommendation, Patches and Settings in the same
tab. The namespace comes from the resource, never from the request, so a
team sees its own namespace and nothing else of the cluster; the cluster,
nodes, agent and namespace-list views refuse namespace resources. A
workload's Apply recommendation button starts the Apply StormForge
Recommendation action (RSA-d2zufawc) as a CloudBolt job for people who may
run that action on the namespace.

Data comes from the StormForge API with the credential in the "StormForge"
connection info (the one the agent install uses), asking for the
view:clusters and view:workloads scopes:
    GET /management/clusters/{cluster}                 cards, namespace table
    GET /management/clusters/{cluster}/config          Config
    GET /management/workloads?cluster=&namespace=      workload tables
    GET /management/namespaces/{cluster}_{namespace}   namespace cards
    GET /management/workloads/{id}                     workload header, Summary
    GET /management/workloads/{id}/config              Settings
    GET /management/workloads/{id}/patches             Patches
    GET /v3/workloads/{cluster}/{ns}/{type}/{name}[/recommendations[/{name}]]
                                                       labels, usage, instance category
StormForge's history charts (usage against requests over time, node
utilization over time) come from the StormForge UI's own backend, which
accepts only signed-in users, so the tabs draw the latest recommendation's
averages instead and link each view to the same page in StormForge. The
Nodes snapshot reads the cluster's Kubernetes API with the credentials of the
container orchestrator CloudBolt registered for it (SHM-2fd3bhsz Kube).

StormForge responses are cached for a minute; Refresh bypasses the cache.
Every endpoint re-checks the viewer's resource.view permission, because the
StormForge credential is a service identity, not the viewer's.

API references:
    https://docs.stormforge.io/docs/reference/api/
    https://docs.stormforge.io/docs/configure/settings/
    https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/node-v1/
    https://kubernetes.io/docs/reference/kubernetes-api/common-definitions/quantity/
    https://github.com/kubernetes-sigs/metrics-server
"""

import base64
import concurrent.futures
import hashlib
import importlib
import json
import math
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from django import forms
from django.contrib import messages
from django.core.cache import cache
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from cbhooks.services.parsing import get_hook_action_input_dict
from common.forms import C2Form
from extensions.views import tab_extension, TabExtensionDelegate
from resources.models import Resource
from utilities.decorators import dialog_view
from utilities.logger import ThreadLogger
from utilities.models import ConnectionInfo

logger = ThreadLogger(__name__)

TEMPLATES = "stormforge_optimize/templates/"

# The stormforge shared module (SHM-2fd3bhsz), imported lazily so a CloudBolt
# without it shows an alert instead of failing to load the extension.
SHARED_MODULE = "shared_modules.stormforge"

VIEW_PERMISSION = "resource.view"
CLUSTER_FIELD = "stormforge_cluster_name"
# Written only after Helm installs the agent (SHM-2fd3bhsz FIELD_VERSION).
AGENT_VERSION_FIELD = "stormforge_agent_version"

# The connection info that enables StormForge (SHM-2fd3bhsz CONNECTION_INFO_NAME).
CONNECTION_INFO = "StormForge"

# Field prefixes of the namespace blueprints' resources (<prefix>name,
# <prefix>cluster_resource_id): SHM-sa5yqm9j, SHM-cozv547n and SHM-kephq8iq.
NAMESPACE_PREFIXES = ("eks_namespace_", "aks_namespace_", "gke_namespace_")

# Plugin (hook) name of the namespace blueprints' apply action, and the
# values its stormforge_apply_mode input accepts (SHM-2fd3bhsz APPLY_MODES).
APPLY_ACTION = "Apply StormForge Recommendation"
APPLY_MODES = (
    ("now", "Apply the latest recommendation now"),
    ("auto_on", "Turn automatic deployment on"),
    ("auto_off", "Turn automatic deployment off"),
)

# Scopes the tabs ask for on the connection info's credential.
READ_SCOPES = ("view:clusters", "view:workloads")
CACHE_SECONDS = 60
CACHE_PREFIX = "sfxui:"

PAGE_SIZES = (10, 25, 50, 100)
DEFAULT_PAGE_SIZE = 25
# Sort fields the StormForge management listings accept.
NAMESPACE_SORTS = ("namespace_name", "workload_count", "impact_cost")
WORKLOAD_SORTS = ("workload_name", "namespace_name", "type", "status", "cpu_throttling_rate", "oom_rate", "impact_cost")

# Kubernetes names: a namespace is an RFC 1123 label, a workload name an RFC
# 1123 subdomain, a workload type a lowercase plural resource (deployments,
# rollouts.argoproj.io).
NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
OBJECT_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
RECOMMENDATION_RE = re.compile(r"^[0-9]{1,20}$")

DONUT_CIRCUMFERENCE = 2 * math.pi * 40

# StormForge's favicon (https://app.stormforge.io/favicons/favicon-32x32.png),
# shown on the tab labels and panel headers.
ICON = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAACXBIWXMAAAsTAAALEwEAmpwYAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAGDSURBVHgB7ZfBkYMwDEWV3Vz2RgmUQAmUQAlQASVAB3DdE3RCCdABdIA78Po7mHESYyBk4j3wZ5SQQbaeLWERopsyYaMw/iFDrGKKLS+4I8svE41HbsQuE4kzfZFjnQDOAa57nMMwtN5njFHXdfNvz/MoCAKj7zAM0q7KEWYT7jdNY/WJ43gG8H1f+uPbpCRJqK5rec0FJT+qqqrmA0bA8r7vrf4CVvnfBkVRxMdx5K8IwcRKZwAsqCxLCaWDYP48z7lIpYS8A4BhkjVykzAhGY7aNE0XITV7HgjKrSBYlcj1bGqOLMvu/LDDZO4Hy80CeRKFwtu23QQEcFNwjLfE2dfBAGWqFQCagkN6gR4GUCl6FAoPgUx6OwCKSZeq+CXptfEWAP3cQDqQ4zUtPSkvAZjyvKZdj+GS4fAoioIfEVL1sBvbAWxCKtTEWOlaWp6O4qMA2BndFzBbAHa9E9raMbog2vFe//Ol9AT4FwCM3Il9i48fYSG50a+6KOnzf89zBP4DGtCCX6RwLgIAAAAASUVORK5CYII="
)

# Tab titles: CloudBolt makes each tab's id from its title, and the EKS, AKS
# and GKE extensions already have a Workloads tab on the same resources, so
# the titles carry an SF prefix that the tab script swaps for the StormForge
# icon.
TABS = {
    "cluster": ("SF Clusters", "Clusters"),
    "namespace": ("SF Namespaces", "Namespaces"),
    "workload": ("SF Workloads", "Workloads"),
    # The one tab of a namespace resource, which already has a Workloads tab.
    "scoped": ("SF Optimization", "Optimization"),
}

ENDPOINTS = {
    "cluster": "sfx_cluster_panel",
    "namespace": "sfx_namespace_panel",
    "workload": "sfx_workload_panel",
    "namespaces_table": "sfx_namespaces_table",
    "workloads_table": "sfx_workloads_table",
    "nodes": "sfx_nodes_panel",
    "patches": "sfx_patches_panel",
    "agent": "sfx_agent_panel",
    "agent_log": "sfx_agent_log",
}

# The agent's namespace and credential Secret (SHM-2fd3bhsz NAMESPACE and AUTH_SECRET).
AGENT_NAMESPACE = "stormforge-system"
AGENT_SECRET = "stormforge-auth"
AGENT_ACTION = "Install or Upgrade StormForge Agent"
LOG_LINES = 200
POD_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


class PanelError(Exception):
    """A readable problem the panel shows instead of a traceback."""


# ---------------------------------------------------------------------------
# StormForge API (read-only, on the "StormForge" connection info)
# ---------------------------------------------------------------------------
_TOKENS = {}
_TOKENS_LOCK = threading.Lock()


def _sf():
    try:
        return importlib.import_module(SHARED_MODULE)
    except ImportError:
        raise PanelError(
            "The StormForge shared module (SHM-2fd3bhsz) is not on this CloudBolt. Sync it from the "
            "repository, then restart the web service."
        )


def _granted_scopes(body, token):
    """The scopes StormForge granted, from the token response or the token's claims; None if unknown.

    A token response names the granted scope when it differs from the request
    (https://www.rfc-editor.org/rfc/rfc6749#section-5.1), and a JWT access
    token carries it as a claim (https://www.rfc-editor.org/rfc/rfc9068#section-2.2.3).
    The claims are read only to explain a refusal; nothing here trusts them.
    """
    if body.get("scope"):
        return set(str(body["scope"]).split())
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return None
    granted = set()
    for key in ("scope", "scp", "permissions"):
        value = claims.get(key) if isinstance(claims, dict) else None
        if isinstance(value, str):
            granted.update(value.split())
        elif isinstance(value, list):
            granted.update(str(v) for v in value)
    return granted if any(k in claims for k in ("scope", "scp", "permissions")) else None


class Reader:
    """GETs against the StormForge API with a token cached per web process."""

    def __init__(self, fresh=False):
        self.sf = _sf()
        try:
            self.client = self.sf.StormForge.from_connection_info()
        except self.sf.StormForgeError as exc:
            raise PanelError(str(exc))
        self.fresh = fresh
        self.granted = None

    @property
    def app_url(self):
        """The StormForge UI beside the API host (api.stormforge.io -> app.stormforge.io)."""
        host = self.client.host
        return f"https://app.{host[len('api.'):]}" if host.startswith("api.") else ""

    def token(self, renew=False):
        key = (self.client.api, self.client.client_id)
        with _TOKENS_LOCK:
            entry = _TOKENS.get(key)
        if entry and not renew and time.time() < entry[1] - 60:
            self.granted = entry[2]
            return entry[0]
        try:
            token_url = (self.client.metadata().get("token_endpoint")
                         or urllib.parse.urljoin(self.client.api, "oauth/token"))
            # Client credentials grant; StormForge grants only the scopes asked for.
            # https://www.rfc-editor.org/rfc/rfc6749#section-4.4
            response = self.sf._http("POST", token_url, data={
                "grant_type": "client_credentials",
                "client_id": self.client.client_id,
                "client_secret": self.client.client_secret,
                "audience": self.client.api,
                "scope": " ".join(READ_SCOPES),
            })
        except self.sf.StormForgeError as exc:
            raise PanelError(str(exc))
        if response.status_code != 200:
            raise PanelError(
                f"StormForge refused the '{self.sf.CONNECTION_INFO_NAME}' connection info credential "
                f"(HTTP {response.status_code}): {self.sf._error_text(response)}. These tabs need the "
                f"{' and '.join(READ_SCOPES)} scopes; a credential created with StormForge's default "
                "permissions has them."
            )
        body = response.json()
        token = body.get("access_token")
        if not token:
            raise PanelError("StormForge returned no access token.")
        self.granted = _granted_scopes(body, token)
        with _TOKENS_LOCK:
            _TOKENS[key] = (token, time.time() + int(body.get("expires_in") or 300), self.granted)
        return token

    def _send(self, url, renew=False):
        headers = {"Authorization": f"Bearer {self.token(renew=renew)}", "Accept": "application/json"}
        try:
            return self.sf._http("GET", url, headers=headers)
        except self.sf.StormForgeError as exc:
            raise PanelError(str(exc))

    def get(self, path, params=None, allow_404=False, what="this data"):
        """Parsed JSON for an API path; None on 404 when ``allow_404``."""
        pairs = [(k, v) for k, v in (params or {}).items() if v not in (None, "")]
        url = urllib.parse.urljoin(self.client.api, path)
        if pairs:
            url += "?" + urllib.parse.urlencode(pairs)
        key = CACHE_PREFIX + hashlib.sha256(url.encode("utf-8")).hexdigest()
        if not self.fresh:
            try:
                hit = cache.get(key)
            except Exception:  # noqa: BLE001 - a cache outage only costs a call
                hit = None
            if hit is not None:
                return hit.get("value")
        response = self._send(url)
        if response.status_code == 401:
            response = self._send(url, renew=True)
        if response.status_code == 404 and allow_404:
            value = None
        elif response.status_code != 200:
            raise PanelError(self._failure(response, what))
        else:
            try:
                value = response.json()
            except ValueError:
                raise PanelError(f"StormForge returned an unreadable response for {what}.")
        try:
            cache.set(key, {"value": value}, CACHE_SECONDS)
        except Exception:  # noqa: BLE001
            logger.debug("Could not cache %s", path)
        return value

    def _failure(self, response, what):
        code = response.status_code
        if code in (401, 403):
            name = self.sf.CONNECTION_INFO_NAME
            reason = self.sf._error_text(response)
            message = f"StormForge denied access to {what} (HTTP {code}{': ' + reason if reason else ''})."
            if self.granted is None:
                message += f" StormForge did not say which scopes it granted the '{name}' credential."
            else:
                missing = [scope for scope in READ_SCOPES if scope not in self.granted]
                have = ", ".join(sorted(self.granted)) or "no scopes"
                message += f" The token StormForge issued to the '{name}' credential carries {have}"
                if missing:
                    message += f" and lacks {', '.join(missing)}."
                else:
                    message += ", which includes every scope these tabs ask for."
            logger.warning("StormForge %s for %s; granted scopes %s", code, what, self.granted)
            return message + (
                f" These tabs need {' and '.join(READ_SCOPES)}; a credential created with StormForge's "
                "default permissions (stormforge auth create) has them."
            )
        if code == 402:
            return "StormForge reports that the organization's subscription does not cover this (HTTP 402)."
        return f"StormForge returned HTTP {code} for {what}: {self.sf._error_text(response)}"


def _parallel(**calls):
    """Run zero-argument callables at once; returns name -> result, re-raising the first failure."""
    if not calls:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(calls))) as pool:
        futures = {name: pool.submit(fn) for name, fn in calls.items()}
    return {name: future.result() for name, future in futures.items()}


def _q(value):
    return urllib.parse.quote(str(value), safe="")


# ---------------------------------------------------------------------------
# Resource and permission helpers
# ---------------------------------------------------------------------------
def _cluster_name(resource):
    """The StormForge cluster name the agent install recorded, or ''."""
    try:
        value = resource.get_cf_values_as_dict().get(CLUSTER_FIELD)
    except Exception:  # noqa: BLE001
        return ""
    value = str(value or "").strip()
    return value if NAMESPACE_RE.match(value) else ""


def _agent_installed(resource):
    try:
        return bool(resource.get_cf_values_as_dict().get(AGENT_VERSION_FIELD))
    except Exception:  # noqa: BLE001
        return False


class Scope:
    """What of StormForge a resource may show.

    A cluster resource shows its whole cluster (``ns`` is ''). A namespace
    resource shows the one namespace it stands for, on its cluster resource's
    StormForge cluster; ``cluster`` is '' when that cluster has no agent.
    """

    def __init__(self, cluster="", ns=""):
        self.cluster = cluster
        self.ns = ns


def _scope(resource):
    try:
        values = resource.get_cf_values_as_dict()
    except Exception:  # noqa: BLE001
        return Scope()
    for prefix in NAMESPACE_PREFIXES:
        ns = str(values.get(prefix + "name") or "").strip()
        if not NAMESPACE_RE.match(ns):
            continue
        parent = resource.parent_resource
        if parent is None:
            parent_id = str(values.get(prefix + "cluster_resource_id") or "").strip()
            parent = Resource.objects.filter(id=int(parent_id)).first() if parent_id.isdigit() else None
        if parent is None or not _agent_installed(parent):
            return Scope(ns=ns)
        return Scope(_cluster_name(parent), ns)
    return Scope(_cluster_name(resource))


def _can_view(profile, resource):
    if getattr(profile, "is_cbadmin", False):
        return True
    try:
        return bool(profile.has_permission(VIEW_PERMISSION, resource))
    except Exception:  # noqa: BLE001
        logger.exception("Permission check failed for resource %s", resource.id)
        return False


def _arg(request, name, pattern):
    value = (request.GET.get(name) or "").strip()
    return value if value and pattern.match(value) else ""


def _workload_args(request):
    ns = _arg(request, "ns", NAMESPACE_RE)
    kind = _arg(request, "type", OBJECT_RE)
    name = _arg(request, "name", OBJECT_RE)
    return (ns, kind, name) if ns and kind and name else ("", "", "")


def _not_registered(cluster):
    return (
        f"StormForge has no cluster named '{cluster}'. The agent registers the cluster when it first "
        "connects; if it never connected, run Install or Upgrade StormForge Agent on this cluster and "
        "check its job log."
    )


def _app_link(reader, *segments):
    base = reader.app_url
    if not base:
        return ""
    return base + "/workloads/" + "/".join(_q(s) for s in segments)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
_QUANTITY_RE = re.compile(r"^([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)([a-zA-Z]*)$")
_SUFFIXES = {
    "": 1, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
    "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50, "Ei": 2 ** 60,
}


def _quantity(text):
    """A Kubernetes quantity ('300m', '256Mi', '2') as a float, or None."""
    match = _QUANTITY_RE.match(str(text or "").strip())
    if not match or match.group(2) not in _SUFFIXES:
        return None
    return float(match.group(1)) * _SUFFIXES[match.group(2)]


def _float(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _trim(number, places=2):
    text = f"{number:.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _sign(value, signed):
    if value is None:
        return ""
    if value < 0:
        return "-"
    return "+" if signed and value > 0 else ""


def _cores(value, signed=False):
    v = _float(value)
    return "--" if v is None else f"{_sign(v, signed)}{abs(v):.3f}"


def _cores_short(value):
    """Cores without trailing zeros, as StormForge's per-pod figures show them (0.3, 0.01)."""
    v = _float(value)
    return "--" if v is None else _trim(v, 3)


def _cpu(value, signed=False):
    """CPU the way kubectl shows it: millicores below one core."""
    v = _float(value)
    if v is None:
        return "--"
    a = abs(v)
    text = f"{round(a * 1000)}m" if a < 1 else _trim(a, 2)
    return _sign(v, signed) + text


def _mem(value, signed=False):
    v = _float(value)
    if v is None:
        return "--"
    a = abs(v)
    if a >= 2 ** 30:
        text = f"{_trim(a / 2 ** 30)} GiB"
    else:
        text = f"{_trim(a / 2 ** 20)} MiB"
    return _sign(v, signed) + text


def _money(value, signed=False):
    v = _float(value)
    return "--" if v is None else f"{_sign(v, signed)}${abs(v):,.2f}"


def _pct(delta, base):
    d, b = _float(delta), _float(base)
    if d is None or not b:
        return ""
    return f"{round(abs(d) / abs(b) * 100)}%"


def _direction(value):
    v = _float(value)
    if not v:
        return ""
    return "down" if v < 0 else "up"


def _rate(value):
    v = _float(value)
    if v is None:
        return "--"
    if v == 0:
        return "0"
    text = _trim(v, 4)
    return text if text != "0" else "<0.0001"


def _score(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _score_class(score):
    if score is None:
        return "none"
    if score < 40:
        return "low"
    if score < 70:
        return "mid"
    return "high"


def _when(value):
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _relative(value):
    """'3 minutes ago' / 'in 24 hours', or ''."""
    when = _when(value)
    if when is None:
        return ""
    seconds = (when - datetime.now(timezone.utc)).total_seconds()
    span = abs(seconds)
    text = "less than a minute"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if span >= size:
            count = int(round(span / size))
            text = f"{count} {unit}{'' if count == 1 else 's'}"
            break
    return f"in {text}" if seconds > 0 else f"{text} ago"


def _stamp(value):
    when = _when(value)
    return when.strftime("%Y-%m-%d %H:%M UTC") if when else ""


_DURATION_RE = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)
_CRON_SHORTCUTS = {
    "@yearly": "Once every year", "@annually": "Once every year", "@monthly": "Once every month",
    "@weekly": "Once every week", "@daily": "Once every day",
    "@midnight": "Once every day, between 12 AM and 3 AM", "@hourly": "Once every hour",
}


def _duration(value, friendly=False):
    """An ISO 8601 duration or cron shortcut in words, as StormForge's UI shows it."""
    value = str(value or "").strip()
    if not value:
        return "--"
    if value.startswith("@"):
        return _CRON_SHORTCUTS.get(value.lower(), value)
    match = _DURATION_RE.match(value.upper())
    if not match or not any(match.groups()):
        return value
    units = ("year", "month", "week", "day", "hour", "minute", "second")
    parts = [f"{int(n)} {unit}{'s' if int(n) > 1 else ''}" for n, unit in zip(match.groups(), units) if n and int(n)]
    if friendly and len(parts) == 1:
        named = {"1 week": "Weekly", "1 day": "Daily", "1 hour": "Hourly"}
        if parts[0] in named:
            return named[parts[0]]
    text = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] if parts else value
    return f"Once every {text}" if friendly else text


# ---------------------------------------------------------------------------
# Shared figures: score, summary, impact
# ---------------------------------------------------------------------------
def _summary(doc):
    """The Optimization Score, Recommendation Summary and Recommendation Impact cards."""
    doc = doc or {}
    score = _score(doc.get("optimization_score"))
    impact_cost = _float(doc.get("impact_cost"))
    return {
        "has_data": score is not None or impact_cost is not None,
        "score": "--" if score is None else f"{score}%",
        "score_cpu": "--" if _score(doc.get("optimization_score_cpu")) is None else f"{_score(doc.get('optimization_score_cpu'))}%",
        "score_memory": "--" if _score(doc.get("optimization_score_memory")) is None else f"{_score(doc.get('optimization_score_memory'))}%",
        "score_class": _score_class(score),
        "dash": f"{(score or 0) / 100 * DONUT_CIRCUMFERENCE:.2f}",
        "circumference": f"{DONUT_CIRCUMFERENCE:.2f}",
        "current_cost": _money(doc.get("current_cost")),
        "recommended_cost": _money(doc.get("recommended_cost")),
        "current_cpu": _cores(doc.get("current_cpu")),
        "recommended_cpu": _cores(doc.get("recommended_cpu")),
        "current_memory": _mem(doc.get("current_memory")),
        "recommended_memory": _mem(doc.get("recommended_memory")),
        "impact_label": "Estimated Cost Increase" if (impact_cost or 0) > 0 else "Estimated Savings",
        "impact_cost": _money(abs(impact_cost)) if impact_cost is not None else "--",
        "impact_cost_pct": _pct(impact_cost, doc.get("current_cost")),
        "impact_cost_dir": _direction(impact_cost),
        "impact_cpu": _cores(doc.get("impact_cpu"), signed=True),
        "impact_cpu_pct": _pct(doc.get("impact_cpu"), doc.get("current_cpu")),
        "impact_cpu_dir": _direction(doc.get("impact_cpu")),
        "impact_memory": _mem(doc.get("impact_memory"), signed=True),
        "impact_memory_pct": _pct(doc.get("impact_memory"), doc.get("current_memory")),
        "impact_memory_dir": _direction(doc.get("impact_memory")),
        "over_cost": _money(doc.get("overprovisioned_cost"), signed=True),
        "over_cpu": _cores(doc.get("overprovisioned_cpu"), signed=True),
        "over_memory": _mem(doc.get("overprovisioned_memory"), signed=True),
        "under_cost": _money(doc.get("underprovisioned_cost"), signed=True),
        "under_cpu": _cores(doc.get("underprovisioned_cpu"), signed=True),
        "under_memory": _mem(doc.get("underprovisioned_memory"), signed=True),
    }


def _row(doc):
    """Requests and net impact columns of a namespace or workload row."""
    impact = _float(doc.get("impact_cost"))
    return {
        "score": "--" if _score(doc.get("optimization_score")) is None else f"{_score(doc.get('optimization_score'))}%",
        "has_requests": _float(doc.get("current_cpu")) is not None or _float(doc.get("current_memory")) is not None,
        "cur_cpu": _cores(doc.get("current_cpu")),
        "rec_cpu": _cores(doc.get("recommended_cpu")),
        "cur_mem": _mem(doc.get("current_memory")),
        "rec_mem": _mem(doc.get("recommended_memory")),
        "cur_cost": _money(doc.get("current_cost")),
        "rec_cost": _money(doc.get("recommended_cost")),
        "imp_cpu": _cores(doc.get("impact_cpu"), signed=True),
        "imp_mem": _mem(doc.get("impact_memory"), signed=True),
        "imp_cost": _money(impact, signed=True),
        "imp_class": "good" if (impact or 0) < 0 else ("bad" if (impact or 0) > 0 else ""),
    }


STATUS_CLASSES = {"ready": "ok", "learning": "info", "warning": "warn", "error": "bad", "unsupported": "muted"}


def _status_class(status):
    return STATUS_CLASSES.get(str(status or "").split("-")[0].lower(), "muted")


def _messages(doc):
    icons = {"Info": "info-circle", "Warning": "exclamation-triangle", "Error": "x-octagon"}
    out = []
    for message in doc.get("status_messages") or []:
        severity = message.get("severity") or "Info"
        out.append({
            "severity": severity,
            "css": {"Warning": "warn", "Error": "bad"}.get(severity, "info"),
            "icon": icons.get(severity, "info-circle"),
            "title": message.get("title") or "",
            "message": message.get("message") or "",
            "items": message.get("listItems") or [],
        })
    return out


# ---------------------------------------------------------------------------
# Tables (namespaces and workloads): search, sort and pagination
# ---------------------------------------------------------------------------
def _table_params(request, sorts):
    sort = request.GET.get("sort") or ""
    direction = request.GET.get("dir") or "desc"
    try:
        offset = max(0, int(request.GET.get("offset") or 0))
    except ValueError:
        offset = 0
    try:
        limit = int(request.GET.get("limit") or DEFAULT_PAGE_SIZE)
    except ValueError:
        limit = DEFAULT_PAGE_SIZE
    return {
        "q": (request.GET.get("q") or "").strip()[:100],
        "sort": sort if sort in sorts else "",
        "dir": direction if direction in ("asc", "desc") else "desc",
        "offset": offset,
        "limit": limit if limit in PAGE_SIZES else DEFAULT_PAGE_SIZE,
    }


def _api_params(params):
    out = {"offset": params["offset"], "limit": params["limit"], "q": params["q"]}
    if params["sort"]:
        out.update(sort_field=params["sort"], sort_direction=params["dir"])
    return out


def _pager(listing, params, noun):
    total = int(listing.get("total") or 0)
    limit = params["limit"]
    offset = params["offset"]
    pages = max(1, math.ceil(total / limit)) if limit else 1
    page = offset // limit + 1 if limit else 1
    return {
        "total": total,
        "noun": noun,
        "page": page,
        "pages": pages,
        "prev": max(0, offset - limit) if offset > 0 else None,
        "next": offset + limit if offset + limit < total else None,
        "limit": limit,
        "sizes": PAGE_SIZES,
    }


def _columns(params, spec):
    """Header cells: (label, sort key or '', css, arrow)."""
    columns = []
    for label, key, css in spec:
        arrow = ""
        if key and params["sort"] == key:
            arrow = "down" if params["dir"] == "desc" else "up"
        elif key == "impact_cost" and not params["sort"]:
            arrow = "down"
        columns.append({"label": label, "key": key, "css": css, "arrow": arrow})
    return columns


def _namespace_table(reader, cluster, params):
    data = reader.get(f"management/clusters/{_q(cluster)}", _api_params(params), allow_404=True,
                      what="the cluster's namespaces")
    if data is None:
        raise PanelError(_not_registered(cluster))
    listing = data.get("namespaces") or {}
    rows = []
    for doc in listing.get("results") or []:
        row = _row(doc)
        row.update(name=doc.get("namespace_name") or "", workloads=doc.get("workload_count") or 0)
        rows.append(row)
    return data, {
        "table": "namespaces_table",
        "rows": rows,
        "params": params,
        "pager": _pager(listing, params, "Namespaces"),
        "columns": _columns(params, (
            ("Namespace Name", "namespace_name", ""),
            ("Workloads", "workload_count", ""),
            ("Score", "", ""),
            ("Total Requests", "", "sfx-req-col"),
            ("Net Impact", "impact_cost", "sfx-impact-col"),
        )),
    }


def _workload_table(reader, cluster, params, ns=""):
    query = _api_params(params)
    query.update(cluster=cluster, namespace=ns)
    listing = reader.get("management/workloads", query, what="the cluster's workloads") or {}
    rows = []
    for doc in listing.get("results") or []:
        row = _row(doc)
        status = doc.get("status") or ""
        reason = doc.get("status_reason") or ""
        row.update(
            name=doc.get("workload_name") or "",
            namespace=doc.get("namespace_name") or "",
            type=doc.get("type") or "",
            hpa=bool(doc.get("hpa")),
            gpu=bool(doc.get("gpu")),
            status=status or "--",
            status_reason=reason.split("-", 1)[1] if "-" in reason else "",
            status_class=_status_class(reason or status),
            auto_deploy=bool(doc.get("auto_deploy")),
            throttling=_rate(doc.get("cpu_throttling_rate")),
            oom=_rate(doc.get("oom_rate")),
        )
        rows.append(row)
    return {
        "table": "workloads_table",
        "rows": rows,
        "params": params,
        "ns": ns,
        "show_namespace": not ns,
        "pager": _pager(listing, params, "Workloads"),
        "columns": _columns(params, (
            ("Workload Name", "workload_name", ""),
            ("Namespace", "namespace_name", "" if not ns else "d-none"),
            ("Type", "type", ""),
            ("Attributes", "", ""),
            ("Score", "", ""),
            ("Status", "status", ""),
            ("Automatic Deployment", "", ""),
            ("Throttling Rate", "cpu_throttling_rate", ""),
            ("OOM Rate", "oom_rate", ""),
            ("Total Requests", "", "sfx-req-col"),
            ("Net Impact", "impact_cost", "sfx-impact-col"),
        )),
    }


# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------
def _config_view(config):
    config = config or {}
    instances = config.get("nodeInstances") or {}
    costs = config.get("estimatedResourceCosts") or {}
    return {
        "labels": sorted((config.get("labels") or {}).items()),
        "category_key": instances.get("categoryKey") or "",
        "categories": [
            {"title": c.get("title") or "", "value": c.get("value") or "", "ratio": c.get("cpuMemoryRatio") or ""}
            for c in instances.get("categoryValues") or []
        ],
        "provider": config.get("provider") or "",
        "uid": config.get("uid") or "",
        "cpu_cost": costs.get("cpuCorePerHour") or "",
        "memory_cost": costs.get("memoryGibibytePerHour") or "",
    }


def _cluster_context(request, resource, reader, cluster):
    params = _table_params(request, NAMESPACE_SORTS)
    reader.token()
    results = _parallel(
        table=lambda: _namespace_table(reader, cluster, params),
        config=lambda: reader.get(f"management/clusters/{_q(cluster)}/config", allow_404=True,
                                  what="the cluster's configuration"),
    )
    detail, table = results["table"]
    events = detail.get("events") or {}
    agent = detail.get("agent_version") or ""
    return {
        "summary": _summary(detail),
        "messages": _messages(detail),
        "agent_version": agent,
        "applier_version": detail.get("applier_version") or "",
        "node_agent_version": detail.get("node_agent_version") or "",
        "workload_count": detail.get("workload_count") or 0,
        "created": _stamp(events.get("cluster_created")),
        "last_seen": _relative(events.get("last_seen")),
        "last_seen_stamp": _stamp(events.get("last_seen")),
        "config": _config_view(results["config"]),
        "table_ctx": table,
        "app_link": _app_link(reader, cluster, "namespaces"),
        "nodes_link": _app_link(reader, cluster, "nodes"),
        "config_link": _app_link(reader, cluster, "node-instances"),
    }


# -- Nodes: a live snapshot from the cluster's Kubernetes API -----------------
SPOT_LABELS = (
    ("cloud.google.com/gke-spot", "true"),
    ("cloud.google.com/gke-preemptible", "true"),
    ("eks.amazonaws.com/capacityType", "SPOT"),
    ("karpenter.sh/capacity-type", "spot"),
    ("kubernetes.azure.com/scalesetpriority", "spot"),
)
POOL_LABELS = (
    "cloud.google.com/gke-nodepool", "eks.amazonaws.com/nodegroup", "kubernetes.azure.com/agentpool",
    "karpenter.sh/nodepool",
)


def _list_all(kube, path, params, pages=20):
    """Every item of a Kubernetes list, 500 at a time."""
    items, token = [], ""
    for _ in range(pages):
        query = dict(params, limit=500)
        if token:
            query["continue"] = token
        listing = kube.get(path, params=query) or {}
        items.extend(listing.get("items") or [])
        token = (listing.get("metadata") or {}).get("continue") or ""
        if not token:
            break
    return items


def _pod_requests(pod):
    cpu = memory = 0.0
    spec = pod.get("spec") or {}
    for container in spec.get("containers") or []:
        requests = (container.get("resources") or {}).get("requests") or {}
        cpu += _quantity(requests.get("cpu")) or 0
        memory += _quantity(requests.get("memory")) or 0
    overhead = spec.get("overhead") or {}
    return cpu + (_quantity(overhead.get("cpu")) or 0), memory + (_quantity(overhead.get("memory")) or 0)


def _gauge(used, requested, capacity, fmt, requested_label="Allocated"):
    def pos(value):
        if value is None or not capacity:
            return None
        return f"{min(value / capacity * 100, 100):.2f}"
    return {
        "used": fmt(used) if used is not None else "--",
        "requested": fmt(requested),
        "capacity": fmt(capacity),
        "used_pct": pos(used),
        "requested_pct": pos(requested),
        "used_share": _pct(used, capacity) if used is not None else "",
        "requested_share": _pct(requested, capacity),
        "requested_label": requested_label,
        "has_used": used is not None,
    }


def _nodes_context(request, resource, reader, cluster):
    sf = reader.sf
    try:
        kube = sf.Kube.for_resource(resource)
    except sf.StormForgeError as exc:
        raise PanelError(str(exc))
    try:
        nodes = (kube.get("/api/v1/nodes") or {}).get("items") or []
        pods = _list_all(kube, "/api/v1/pods", {"fieldSelector": "status.phase!=Succeeded,status.phase!=Failed"})
    except sf.StormForgeError as exc:
        raise PanelError(f"Could not read the cluster's nodes from Kubernetes: {exc}")
    # metrics-server: GKE and AKS run it by default; EKS only when installed.
    try:
        metrics = kube.get("/apis/metrics.k8s.io/v1beta1/nodes") or {}
    except sf.StormForgeError:
        metrics = {}
    usage = {
        (item.get("metadata") or {}).get("name"): item.get("usage") or {}
        for item in metrics.get("items") or []
    }

    per_node = {}
    for pod in pods:
        node_name = (pod.get("spec") or {}).get("nodeName")
        if not node_name:
            continue
        cpu, memory = _pod_requests(pod)
        entry = per_node.setdefault(node_name, [0.0, 0.0, 0])
        entry[0] += cpu
        entry[1] += memory
        entry[2] += 1

    rows, types = [], {}
    totals = {"cpu_cap": 0.0, "mem_cap": 0.0, "pods_cap": 0.0, "cpu_req": 0.0, "mem_req": 0.0, "pods": 0,
              "cpu_used": 0.0, "mem_used": 0.0}
    for node in nodes:
        meta = node.get("metadata") or {}
        labels = meta.get("labels") or {}
        status = node.get("status") or {}
        capacity = status.get("capacity") or {}
        allocatable = status.get("allocatable") or {}
        name = meta.get("name") or ""
        itype = labels.get("node.kubernetes.io/instance-type") or labels.get("beta.kubernetes.io/instance-type") or "unknown"
        spot = any(labels.get(key) == value for key, value in SPOT_LABELS)
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
        cpu_alloc = _quantity(allocatable.get("cpu")) or 0
        mem_alloc = _quantity(allocatable.get("memory")) or 0
        pods_alloc = _quantity(allocatable.get("pods")) or 0
        requested = per_node.get(name, [0.0, 0.0, 0])
        node_usage = usage.get(name) or {}
        cpu_used = _quantity(node_usage.get("cpu"))
        mem_used = _quantity(node_usage.get("memory"))
        totals["cpu_cap"] += _quantity(capacity.get("cpu")) or 0
        totals["mem_cap"] += _quantity(capacity.get("memory")) or 0
        totals["pods_cap"] += pods_alloc
        totals["cpu_req"] += requested[0]
        totals["mem_req"] += requested[1]
        totals["pods"] += requested[2]
        totals["cpu_used"] += cpu_used or 0
        totals["mem_used"] += mem_used or 0
        kind = types.setdefault(itype, {"type": itype, "count": 0, "spot": 0})
        kind["count"] += 1
        kind["spot"] += 1 if spot else 0
        rows.append({
            "name": name,
            "type": itype,
            "pool": next((labels[key] for key in POOL_LABELS if labels.get(key)), ""),
            "zone": labels.get("topology.kubernetes.io/zone") or labels.get("failure-domain.beta.kubernetes.io/zone") or "",
            "category": labels.get("stormforge.io/instance-category") or "",
            "spot": spot,
            "ready": ready,
            "cpu": f"{_cpu(requested[0])} / {_cpu(cpu_alloc)}",
            "cpu_used": _cpu(cpu_used) if cpu_used is not None else "--",
            "memory": f"{_mem(requested[1])} / {_mem(mem_alloc)}",
            "memory_used": _mem(mem_used) if mem_used is not None else "--",
            "pods": f"{requested[2]} / {int(pods_alloc)}",
        })
    has_usage = bool(usage)
    biggest = max([t["count"] for t in types.values()] or [1])
    type_rows = sorted(types.values(), key=lambda t: (-t["count"], t["type"]))
    for t in type_rows:
        t["pct"] = f"{t['count'] / biggest * 100:.2f}"
    return {
        "node_count": len(nodes),
        "has_usage": has_usage,
        "cpu": _gauge(totals["cpu_used"] if has_usage else None, totals["cpu_req"], totals["cpu_cap"], lambda v: f"{_trim(v)} cores"),
        "memory": _gauge(totals["mem_used"] if has_usage else None, totals["mem_req"], totals["mem_cap"], _mem),
        "pods": _gauge(None, totals["pods"], totals["pods_cap"], lambda v: str(int(round(v))), "Running"),
        "types": type_rows,
        "rows": sorted(rows, key=lambda r: r["name"]),
        "nodes_link": _app_link(reader, cluster, "nodes"),
        "orchestrator": resource.get_cf_values_as_dict().get("container_orchestrator_id") or "",
    }


# -- Agent: StormForge's cluster record, the agent pods and their log ---------
def _agent_action(profile, resource):
    """The Install or Upgrade StormForge Agent action when the viewer may run it here, else None."""
    try:
        from cbhooks.models import ResourceAction
        from utilities.permissions import resource_action_permitted
    except ImportError:
        return None
    try:
        for action in ResourceAction.objects.filter(enabled=True, label=AGENT_ACTION):
            blueprints = list(action.blueprints.values_list("id", flat=True))
            if blueprints and resource.blueprint_id not in blueprints:
                continue
            if resource_action_permitted(profile, resource, action):
                return action
    except Exception:  # noqa: BLE001 - a missing action only hides the button
        logger.exception("Could not resolve the '%s' action for resource %s", AGENT_ACTION, resource.id)
    return None


def _can_read_agent_log(profile, resource):
    """CloudBolt admins and people who may run Install or Upgrade StormForge Agent on the cluster."""
    return bool(getattr(profile, "is_cbadmin", False)) or _agent_action(profile, resource) is not None


def _kube(reader, resource):
    try:
        return reader.sf.Kube.for_resource(resource)
    except reader.sf.StormForgeError as exc:
        raise PanelError(str(exc))


def _secret_written(secret):
    """When the credential Secret was last written: the newest managedFields time
    (https://kubernetes.io/docs/reference/using-api/server-side-apply/#field-management)."""
    metadata = (secret or {}).get("metadata") or {}
    times = [_when(entry.get("time")) for entry in metadata.get("managedFields") or []]
    times = [t for t in times if t] or [_when(metadata.get("creationTimestamp"))]
    return max((t for t in times if t), default=None)


def _container_state(state):
    state = state or {}
    if "running" in state:
        return "Running", "ok"
    if "waiting" in state:
        return (state["waiting"] or {}).get("reason") or "Waiting", "warn"
    if "terminated" in state:
        terminated = state["terminated"] or {}
        return terminated.get("reason") or "Terminated", "bad" if terminated.get("exitCode") else "muted"
    return "--", "muted"


def _agent_pods(kube):
    """Pods in stormforge-system run by a Deployment, DaemonSet or StatefulSet (not CloudBolt's Helm Jobs)."""
    listing = kube.get(f"/api/v1/namespaces/{AGENT_NAMESPACE}/pods") or {}
    return [
        pod for pod in listing.get("items") or []
        if any(ref.get("kind") in ("ReplicaSet", "DaemonSet", "StatefulSet")
               for ref in (pod.get("metadata") or {}).get("ownerReferences") or [])
    ]


def _default_log_target(pods):
    """(pod, container) whose log the panel opens with: the agent's own container when it can tell."""
    def score(pod):
        name = (pod.get("metadata") or {}).get("name") or ""
        return (0 if name.startswith("stormforge-agent") else 1 if "agent" in name else 2, name)
    for pod in sorted(pods, key=score):
        containers = [c.get("name") for c in (pod.get("spec") or {}).get("containers") or [] if c.get("name")]
        if containers:
            preferred = next((c for c in containers if "agent" in c), containers[0])
            return pod["metadata"]["name"], preferred
    return "", ""


def _record_view(record):
    status = (record or {}).get("status") or {}
    phase = status.get("phase") or "--"
    conditions, last_seen = [], None
    for condition in status.get("conditions") or []:
        kind = condition.get("type") or ""
        if kind == "stormforge.io/active":
            last_seen = condition.get("lastProbeTime")
        state = str(condition.get("status") or "")
        conditions.append({
            "type": kind.split("/")[-1].replace("-", " ").capitalize() or "--",
            "raw": kind,
            "status": state or "--",
            "css": "ok" if state == "True" else ("bad" if state == "False" else "muted"),
            "message": condition.get("message") or condition.get("reason") or "",
            "probe": _relative(condition.get("lastProbeTime")),
            "since": _stamp(condition.get("lastTransitionTime")),
        })
    products = [
        {"name": p.get("name") or "--", "version": p.get("version") or "--"}
        for p in ((record or {}).get("stormforge") or {}).get("products") or []
    ]
    kubernetes = (record or {}).get("kubernetes") or {}
    return {
        "phase": phase,
        "phase_class": {"Created": "ok", "Active": "ok", "Inactive": "warn"}.get(phase, "muted"),
        "last_seen": _relative(last_seen),
        "last_seen_stamp": _stamp(last_seen),
        "conditions": conditions,
        "products": products,
        "k8s_version": kubernetes.get("version") or "--",
        "provider": kubernetes.get("provider") or "--",
        "uid": kubernetes.get("systemNamespaceUID") or "",
        "nodes": len((record or {}).get("nodes") or []),
    }


def _agent_context(request, resource, reader, cluster):
    profile = request.get_user_profile()
    reader.token()
    results = _parallel(
        record=lambda: reader.get(f"v3/clusters/{_q(cluster)}", allow_404=True,
                                  what="StormForge's record of the cluster"),
        detail=lambda: reader.get(f"management/clusters/{_q(cluster)}", {"limit": 1}, allow_404=True,
                                  what="the cluster's workloads in StormForge"),
    )
    record = results["record"]
    context = {
        "registered": record is not None,
        "record": _record_view(record) if record is not None else None,
        "workload_count": (results["detail"] or {}).get("workload_count") or 0,
        "app_link": _app_link(reader, cluster, "namespaces"),
    }
    action = _agent_action(profile, resource)
    context["action_url"] = reverse("run_resource_action", args=[resource.id, action.id]) if action else ""
    context["can_read_log"] = bool(getattr(profile, "is_cbadmin", False)) or action is not None

    # The cluster side; a failure here leaves StormForge's view on the page.
    try:
        kube = _kube(reader, resource)
        kube_system = kube.get("/api/v1/namespaces/kube-system") or {}
        secret = kube.get(f"/api/v1/namespaces/{AGENT_NAMESPACE}/secrets/{AGENT_SECRET}")
        pods = _agent_pods(kube)
    except (PanelError, reader.sf.StormForgeError) as exc:
        context["kube_error"] = f"Could not read the agent from the cluster: {exc}"
        return context
    written = _secret_written(secret)
    metadata = (secret or {}).get("metadata") or {}
    labels, annotations = metadata.get("labels") or {}, metadata.get("annotations") or {}
    secret = metadata = None  # only labels and timestamps are used; the credential stays here
    if labels.get("app.kubernetes.io/managed-by") == "cloudbolt":
        secret_owner = "CloudBolt"
    elif labels.get("app.kubernetes.io/managed-by") == "Helm" or "meta.helm.sh/release-name" in annotations:
        secret_owner = "the StormForge chart"
    else:
        secret_owner = "someone outside CloudBolt" if written else ""

    rows, stale = [], 0
    for pod in sorted(pods, key=lambda p: (p.get("metadata") or {}).get("name") or ""):
        meta, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
        statuses = {c.get("name"): c for c in status.get("containerStatuses") or []}
        started = _when(status.get("startTime"))
        is_stale = bool(written and started and started < written)
        stale += 1 if is_stale else 0
        containers = []
        for container in spec.get("containers") or []:
            cstatus = statuses.get(container.get("name")) or {}
            state, css = _container_state(cstatus.get("state"))
            last = (cstatus.get("lastState") or {}).get("terminated") or {}
            containers.append({
                "name": container.get("name") or "",
                "image": (container.get("image") or "").rsplit("/", 1)[-1],
                "state": state,
                "css": css,
                "restarts": int(cstatus.get("restartCount") or 0),
                "last": (f"{last.get('reason') or 'Terminated'} (exit {last.get('exitCode')}) "
                         f"{_relative(last.get('finishedAt'))}").strip() if last else "",
            })
        ready = sum(1 for c in statuses.values() if c.get("ready"))
        rows.append({
            "name": meta.get("name") or "",
            "phase": status.get("phase") or "--",
            "phase_class": {"Running": "ok", "Pending": "warn", "Failed": "bad"}.get(status.get("phase"), "muted"),
            "ready": f"{ready}/{len(spec.get('containers') or [])}",
            "restarts": sum(c["restarts"] for c in containers),
            "started": _relative(status.get("startTime")),
            "started_stamp": _stamp(status.get("startTime")),
            "stale": is_stale,
            "containers": containers,
        })
    log_pod, log_container = _default_log_target(pods)
    ours = (kube_system.get("metadata") or {}).get("uid") or ""
    theirs = (context["record"] or {}).get("uid") or ""
    context.update(
        pods=rows,
        stale=stale,
        secret_present=bool(written),
        secret_written=_relative(written.isoformat()) if written else "",
        secret_written_stamp=written.strftime("%Y-%m-%d %H:%M UTC") if written else "",
        secret_owner=secret_owner,
        identity="match" if ours and theirs and ours == theirs else ("mismatch" if ours and theirs else ""),
        log_targets=[
            {"pod": (p.get("metadata") or {}).get("name"), "container": c.get("name"),
             "selected": (p.get("metadata") or {}).get("name") == log_pod and c.get("name") == log_container}
            for p in pods for c in (p.get("spec") or {}).get("containers") or []
        ],
        log_pod=log_pod,
        log_container=log_container,
    )
    return context


def _agent_log_context(request, resource, reader, cluster):
    if not _can_read_agent_log(request.get_user_profile(), resource):
        raise PanelError(
            f"Only CloudBolt admins and people who may run {AGENT_ACTION} on this cluster can read the agent log."
        )
    pod_name = _arg(request, "pod", POD_RE)
    container = _arg(request, "container", NAMESPACE_RE)
    previous = request.GET.get("previous") == "1"
    kube = _kube(reader, resource)
    try:
        pods = {(p.get("metadata") or {}).get("name"): p for p in _agent_pods(kube)}
    except reader.sf.StormForgeError as exc:
        raise PanelError(f"Could not read the agent pods: {exc}")
    pod = pods.get(pod_name)
    if pod is None:
        raise PanelError("Choose one of the agent pods above.")
    names = [c.get("name") for c in (pod.get("spec") or {}).get("containers") or []]
    if container not in names:
        container = names[0] if names else ""
    params = {"container": container, "tailLines": str(LOG_LINES), "timestamps": "true"}
    if previous:
        params["previous"] = "true"
    # https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/#get-read-log-of-the-specified-pod
    try:
        text = kube.request("GET", f"/api/v1/namespaces/{AGENT_NAMESPACE}/pods/{pod_name}/log",
                            params=params, allow_404=True, text=True)
    except reader.sf.StormForgeError as exc:
        raise PanelError(f"Kubernetes did not return the {'previous ' if previous else ''}log of "
                         f"{pod_name}/{container}: {exc}")
    return {
        "pod": pod_name,
        "container": container,
        "previous": previous,
        "lines": LOG_LINES,
        "log": text if isinstance(text, str) else "",
    }


# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------
def _namespace_context(request, resource, reader, cluster):
    ns = _arg(request, "ns", NAMESPACE_RE)
    if not ns:
        params = _table_params(request, NAMESPACE_SORTS)
        _detail, table = _namespace_table(reader, cluster, params)
        return {"picker": True, "table_ctx": table, "app_link": _app_link(reader, cluster, "namespaces")}
    params = _table_params(request, WORKLOAD_SORTS)
    reader.token()
    results = _parallel(
        detail=lambda: reader.get(f"management/namespaces/{_q(cluster + '_' + ns)}", {"limit": 1},
                                  what=f"namespace {ns}"),
        table=lambda: _workload_table(reader, cluster, params, ns),
    )
    detail = results["detail"] or {}
    return {
        "picker": False,
        "ns": ns,
        "summary": _summary(detail),
        "workload_count": detail.get("workload_count") or 0,
        "table_ctx": results["table"],
        "app_link": _app_link(reader, cluster, ns),
    }


# ---------------------------------------------------------------------------
# Workload
# ---------------------------------------------------------------------------
GOAL_TEXT = {
    "Savings": ("currency-dollar", "Maximizes your resource savings. Some bursting can be expected."),
    "Balanced": ("symmetry-vertical", "Provides a balanced approach to achieving cost savings and workload reliability."),
    "Reliability": ("speedometer2", "Minimizes the chance of usage exceeding CPU and memory requests."),
}

APPLY_METHODS = {
    "PatchImmediateRollout": (
        "Patch · Immediate Rollout",
        "Use server-side apply patches to modify Deployments, StatefulSets, and other Pod-owning workload "
        "resources directly whenever a new recommendation is applied."),
    "WebhookImmediateRollout": (
        "Webhook · Immediate Rollout",
        "Use a mutating webhook to modify Pods at admission. Existing pods will be resized in place whenever a "
        "new recommendation is applied. If pods cannot be resized, a rollout/restart will be triggered."),
    "WebhookHybridRollout": (
        "Webhook · Hybrid Rollout",
        "Use a mutating webhook to modify Pods at admission. Existing pods will be resized in place whenever a "
        "new recommendation is applied. If pods cannot be resized, changes will be deferred until the next "
        "normal rollout."),
    "WebhookDeferredRollout": (
        "Webhook · Deferred Rollout",
        "Use a mutating webhook to modify Pods at admission. Existing pods will not be modified and the "
        "workload will not restart when recommendations are applied. Changes will be deferred until the next "
        "time pods restart."),
}

BUMP_UP_APPLY = {
    "IfAutoDeployEnabled": "If Auto-Deploy Enabled",
    "Always": "Always",
    "Never": "Never",
}

PRESERVE_SCALE_POINT = {
    "True": "Recommendations never cause the HPA scale point to change.",
    "False": "Recommendations may cause the HPA scale point to change.",
    "UnlessUnderProvisioned": "Recommendations never reduce resource requests in a way that causes the HPA "
                              "scale point to change.",
}

POLICY_LABELS = {"DoNotOptimize": "Don't Optimize", "InstanceCategories": "Instance Categories"}

# Workload resource types as StormForge's page titles name them.
KIND_LABELS = {
    "deployments": "Deployment", "statefulsets": "StatefulSet", "daemonsets": "DaemonSet",
    "replicasets": "ReplicaSet", "jobs": "Job", "cronjobs": "CronJob", "rollouts.argoproj.io": "Rollout",
}

# StormForge's own annotation examples for each settings page. The Runtime
# examples hold Go template braces, so they are joined here rather than
# written into a Django template.
_OPEN, _CLOSE = "{" * 2, "}" * 2
ANNOTATIONS = {
    "goals": (
        'live.stormforge.io/cpu.optimization-goal: "Savings"',
        'live.stormforge.io/memory.optimization-goal: "Reliability"',
    ),
    "schedule": (
        'live.stormforge.io/schedule: "@daily"',
        'live.stormforge.io/validity-period: "P14D"',
    ),
    "apply": (
        'live.stormforge.io/auto-deploy: "Enabled"',
        'live.stormforge.io/learning-period: "P3D"',
        'live.stormforge.io/auto-deploy.thresholds.cpu.unit: "10m"',
        'live.stormforge.io/auto-deploy.thresholds.memory.unit: "64Mi"',
        'live.stormforge.io/auto-deploy.thresholds.cpu.percent: "5"',
        'live.stormforge.io/auto-deploy.thresholds.memory.percent: "20"',
        'live.stormforge.io/apply.method: "PatchImmediateRollout"',
        'live.stormforge.io/apply.max-percent-decrease: "20"',
    ),
    "reliability": (
        'live.stormforge.io/reliability.oom.memory-bump-up.enabled: "true"',
        'live.stormforge.io/reliability.oom.memory-bump-up.period: "P1D"',
        'live.stormforge.io/reliability.oom.memory-bump-up.percent: "20"',
        'live.stormforge.io/reliability.oom.memory-bump-up.min: "100Mi"',
        'live.stormforge.io/reliability.oom.memory-bump-up.max: "2Gi"',
        'live.stormforge.io/reliability.oom.memory-bump-up.apply-immediately: "IfAutoDeployEnabled"',
        'live.stormforge.io/reliability.cpu-pressure.cpu-bump-up.enabled: "true"',
        'live.stormforge.io/reliability.cpu-pressure.cpu-bump-up.period: "P1D"',
        'live.stormforge.io/reliability.cpu-pressure.cpu-bump-up.percent: "20"',
        'live.stormforge.io/reliability.cpu-pressure.cpu-bump-up.min: "100m"',
        'live.stormforge.io/reliability.cpu-pressure.cpu-bump-up.max: "2"',
        'live.stormforge.io/reliability.cpu-pressure.cpu-bump-up.apply-immediately: "IfAutoDeployEnabled"',
    ),
    "resources": (
        'live.stormforge.io/containers.cpu.optimization-policy: "RequestsRaiseLimitsIfNeeded"',
        'live.stormforge.io/containers.cpu.limits.limit-request-ratio: "2.0"',
        'live.stormforge.io/containers.cpu.requests.min: "10m"',
        'live.stormforge.io/containers.cpu.requests.max: "16000m"',
        'live.stormforge.io/containers.cpu.limits.min: "10m"',
        'live.stormforge.io/containers.cpu.limits.max: "16000m"',
        'live.stormforge.io/containers.memory.optimization-policy: "RequestsRaiseLimitsIfNeeded"',
        'live.stormforge.io/containers.memory.limits.limit-request-ratio: "3.0"',
        'live.stormforge.io/containers.memory.requests.min: "256Mi"',
        'live.stormforge.io/containers.memory.requests.max: "54Gi"',
        'live.stormforge.io/containers.memory.limits.min: "384Mi"',
        'live.stormforge.io/containers.memory.limits.max: "64Gi"',
    ),
    "hpa": (
        'live.stormforge.io/hpa.cpu.target-utilization.min: "50"',
        'live.stormforge.io/hpa.cpu.target-utilization.max: "75"',
        'live.stormforge.io/hpa.memory.target-utilization.min: "50"',
        'live.stormforge.io/hpa.memory.target-utilization.max: "75"',
        'live.stormforge.io/hpa.preserve-horizontal-scale-point: "False"',
        'live.stormforge.io/hpa.ignore-horizontally-scaled-resources: "True"',
    ),
    "runtime": (
        "live.stormforge.io/containers.java.max-heap.patch-path: '"
        + _OPEN + ' .EnvVarPath "STORMFORGE_JAVA_ARGS" ' + _CLOSE + "'",
        "live.stormforge.io/containers.java.max-heap.patch-format: '"
        + _OPEN + ' jvmOption "-XX:MaxHeapSize" .Value ' + _CLOSE + "'",
        'live.stormforge.io/containers.java.max-heap.min: "128Mi"',
        'live.stormforge.io/containers.java.max-heap.max: "8192Gi"',
    ),
    "scheduling": (
        'live.stormforge.io/pod-scheduling.optimization-policy: "DoNotOptimize"',
        'live.stormforge.io/pod-scheduling.instance-categories.node-affinity.type: '
        '"PreferredDuringSchedulingIgnoredDuringExecution"',
        'live.stormforge.io/pod-scheduling.instance-categories.node-affinity.weight: "70"',
    ),
}


def _dash(value):
    value = "" if value is None else str(value).strip()
    return value or "--"


def _container_settings(title, note, settings):
    settings = settings or {}

    def side(resource):
        values = settings.get(resource) or {}
        return {
            "policy": _dash(values.get("optimizationPolicy")),
            "ratio": _dash(values.get("limitRequestRatio")),
            "req_min": _dash(values.get("requestsMin")),
            "req_max": _dash(values.get("requestsMax")),
            "lim_min": _dash(values.get("limitsMin")),
            "lim_max": _dash(values.get("limitsMax")),
        }

    java = settings.get("java") or {}
    return {
        "title": title,
        "note": note,
        "cpu": side("cpu"),
        "memory": side("memory"),
        "java": {
            "path": _dash(java.get("maxHeapPatchPath")),
            "format": _dash(java.get("maxHeapPatchFormat")),
            "min": _dash(java.get("maxHeapMin")),
            "max": _dash(java.get("maxHeapMax")),
        },
    }


def _bump_up(values, kind):
    values = values or {}
    return {
        "kind": kind,
        "enabled": values.get("enabled") is True,
        "apply": BUMP_UP_APPLY.get(values.get("applyImmediately") or "", _dash(values.get("applyImmediately"))),
        "apply_default": (values.get("applyImmediately") or "Never") == "Never",
        "period": _duration(values.get("period")),
        "percent": f"{values['percent']}%" if values.get("percent") else "--",
        "min": _dash(values.get("min")),
        "max": _dash(values.get("max")),
    }


def _settings(config):
    config = config or {}
    schedule = config.get("schedule") or {}
    auto = config.get("autoDeploy") or {}
    thresholds = auto.get("thresholds") or {}
    apply = config.get("apply") or {}
    method = apply.get("method") or "PatchImmediateRollout"
    reliability = config.get("reliability") or {}
    hpa = config.get("hpa") or {}
    scheduling = config.get("podScheduling") or {}
    categories = scheduling.get("instanceCategories") or {}
    policy = scheduling.get("schedulingOptimizationPolicy") or "DoNotOptimize"
    cpu_goal = (config.get("cpu") or {}).get("optimizationGoal") or "Balanced"
    memory_goal = (config.get("memory") or {}).get("optimizationGoal") or "Balanced"
    affinity = categories.get("nodeAffinityType") or "PreferredDuringSchedulingIgnoredDuringExecution"
    weight = categories.get("nodeAffinityWeight")
    weight = 70 if weight in (None, "") else weight
    containers = [
        _container_settings(f"Container name: {c.get('name') or '--'}", "", c)
        for c in config.get("containers") or []
    ]
    return {
        "view_only": bool(config.get("viewOnly")),
        "managed_in_cluster": bool(config.get("managedInCluster")),
        "has_java": bool(config.get("hasJava")),
        "goals": {
            "cpu": cpu_goal, "cpu_icon": GOAL_TEXT.get(cpu_goal, ("", ""))[0], "cpu_default": cpu_goal == "Balanced",
            "memory": memory_goal, "memory_icon": GOAL_TEXT.get(memory_goal, ("", ""))[0],
            "memory_default": memory_goal == "Balanced",
            "options": [{"name": k, "icon": v[0], "text": v[1]} for k, v in GOAL_TEXT.items()],
        },
        "schedule": {
            "expression": _duration(schedule.get("expression"), friendly=True),
            "expression_default": (schedule.get("expression") or "P1D").startswith("P1D"),
            "timezone": _dash(schedule.get("timezone")),
            "timezone_default": (schedule.get("timezone") or "UTC") == "UTC",
            "validity": _duration(config.get("validityPeriod")) if config.get("validityPeriod") else "",
        },
        "apply": {
            "auto_deploy": auto.get("enabled") is True,
            "learning": _duration(config.get("learningPeriod") or "P7D"),
            "cpu_unit": _dash((thresholds.get("cpu") or {}).get("unit")),
            "memory_unit": _dash((thresholds.get("memory") or {}).get("unit")),
            "cpu_percent": _dash((thresholds.get("cpu") or {}).get("percent")),
            "memory_percent": _dash((thresholds.get("memory") or {}).get("percent")),
            "method": APPLY_METHODS.get(method, (method, ""))[0],
            "method_text": APPLY_METHODS.get(method, (method, ""))[1],
            "method_default": method == "PatchImmediateRollout",
            "max_decrease": _dash(apply.get("maxPercentDecrease")),
            "max_increase": _dash(apply.get("maxPercentIncrease")),
        },
        "reliability": [
            _bump_up((reliability.get("oom") or {}).get("memoryBumpUp"), "memory"),
            _bump_up((reliability.get("cpuPressure") or {}).get("cpuBumpUp"), "cpu"),
        ],
        "default_container": _container_settings(
            "Default Container Values",
            "Any container which is not listed by name will be optimized using the default container settings.",
            config.get("defaultContainer"),
        ),
        "containers": containers,
        "hpa": {
            "state": "ignored" if hpa.get("ignore") else ("detected" if hpa.get("enabled") else "none"),
            "cpu_min": _dash((hpa.get("cpu") or {}).get("targetUtilizationMin")),
            "cpu_max": _dash((hpa.get("cpu") or {}).get("targetUtilizationMax")),
            "memory_min": _dash((hpa.get("memory") or {}).get("targetUtilizationMin")),
            "memory_max": _dash((hpa.get("memory") or {}).get("targetUtilizationMax")),
            "preserve": _dash(hpa.get("preserveHorizontalScalePoint")),
            "preserve_text": PRESERVE_SCALE_POINT.get(hpa.get("preserveHorizontalScalePoint") or "", ""),
            "ignore_scaled": bool(hpa.get("ignoreHorizontallyScaledResources")),
        },
        "scheduling": {
            "policy": POLICY_LABELS.get(policy, policy),
            "optimizing": policy != "DoNotOptimize",
            "affinity": affinity,
            "affinity_default": affinity == "PreferredDuringSchedulingIgnoredDuringExecution",
            "weight": weight,
            "weight_default": str(weight) == "70",
        },
        "annotations": {key: "\n".join(lines) for key, lines in ANNOTATIONS.items()},
    }


def _bars(kind, usage, cur_req, rec_req, cur_lim, rec_lim):
    """One resource's horizontal chart: average usage as a bar, requests and limits as markers."""
    fmt = _cpu if kind == "cpu" else _mem
    values = [v for v in (usage, cur_req, rec_req, cur_lim, rec_lim) if v]
    top = max(values) * 1.08 if values else 0

    def pos(value):
        return f"{min(value / top * 100, 100):.2f}" if (value and top) else None

    markers = []
    for key, label, value in (
        ("cur-req", "Current request", cur_req),
        ("rec-req", "Recommended request", rec_req),
        ("cur-lim", "Current limit", cur_lim),
        ("rec-lim", "Recommended limit", rec_lim),
    ):
        markers.append({"key": key, "label": label, "value": fmt(value) if value is not None else "--",
                        "pct": pos(value)})
    return {
        "kind": "CPU" if kind == "cpu" else "Memory",
        "usage": fmt(usage) if usage is not None else "--",
        "usage_pct": pos(usage),
        "markers": markers,
        "scale": fmt(top) if top else "",
    }


def _latest_recommendation(reader, cluster, ns, kind, name):
    base = f"v3/workloads/{_q(cluster)}/{_q(ns)}/{_q(kind)}/{_q(name)}/recommendations"
    index = reader.get(base, allow_404=True, what="the workload's recommendations") or {}
    names = [
        item.get("name") for item in index.get("recommendations") or []
        if RECOMMENDATION_RE.match(str(item.get("name") or "")) and item.get("state") not in ("Deleted",)
    ]
    if not names:
        return None
    latest = max(names, key=int)
    return reader.get(f"{base}/{_q(latest)}", allow_404=True, what="the workload's recommendation")


def _containers(detail, recommendation, categories):
    """Container rows of the Recommendation tab and the per-pod Summary figures."""
    pod = ((recommendation or {}).get("pods") or [{}])[0]
    observed = pod.get("observed") or {}
    usage_by_name = {
        c.get("containerName"): (c.get("usage") or {})
        for c in (observed.get("metrics") or {}).get("containerResources") or []
    }
    rows = []
    pod_totals = {"cur_cpu": 0.0, "rec_cpu": 0.0, "cur_mem": 0.0, "rec_mem": 0.0, "use_cpu": 0.0, "use_mem": 0.0,
                  "cur_cpu_lim": 0.0, "rec_cpu_lim": 0.0, "cur_mem_lim": 0.0, "rec_mem_lim": 0.0}
    seen_usage = False
    def no_zero_limit(side):
        # StormForge reports "no limit" as a limit of 0.
        side = dict(side or {})
        for key in ("currentLimits", "recommendedLimits"):
            if _quantity(side.get(key)) == 0:
                side[key] = ""
        return side

    for container in detail.get("containers") or []:
        cpu = no_zero_limit(container.get("cpu"))
        memory = no_zero_limit(container.get("memory"))
        values = {
            "cur_cpu": _quantity(cpu.get("currentRequests")), "rec_cpu": _quantity(cpu.get("recommendedRequests")),
            "cur_cpu_lim": _quantity(cpu.get("currentLimits")), "rec_cpu_lim": _quantity(cpu.get("recommendedLimits")),
            "cur_mem": _quantity(memory.get("currentRequests")), "rec_mem": _quantity(memory.get("recommendedRequests")),
            "cur_mem_lim": _quantity(memory.get("currentLimits")), "rec_mem_lim": _quantity(memory.get("recommendedLimits")),
        }
        if not any(v is not None for v in values.values()):
            continue
        usage = usage_by_name.get(container.get("name")) or {}
        use_cpu, use_mem = _float(usage.get("cpu")), _float(usage.get("memory"))
        seen_usage = seen_usage or use_cpu is not None or use_mem is not None
        for key, value in values.items():
            pod_totals[key] += value or 0
        pod_totals["use_cpu"] += use_cpu or 0
        pod_totals["use_mem"] += use_mem or 0

        def change(current, recommended):
            if current is None or recommended is None or not current:
                return {"pct": "", "dir": ""}
            return {"pct": _pct(recommended - current, current), "dir": _direction(recommended - current)}

        rows.append({
            "name": container.get("name") or "",
            "cpu_req": f"{_dash(cpu.get('currentRequests'))} → {_dash(cpu.get('recommendedRequests'))}",
            "cpu_req_change": change(values["cur_cpu"], values["rec_cpu"]),
            "cpu_lim": f"{_dash(cpu.get('currentLimits'))} → {_dash(cpu.get('recommendedLimits'))}",
            "mem_req": f"{_dash(memory.get('currentRequests'))} → {_dash(memory.get('recommendedRequests'))}",
            "mem_req_change": change(values["cur_mem"], values["rec_mem"]),
            "mem_lim": f"{_dash(memory.get('currentLimits'))} → {_dash(memory.get('recommendedLimits'))}",
            "charts": [
                _bars("cpu", use_cpu, values["cur_cpu"], values["rec_cpu"], values["cur_cpu_lim"], values["rec_cpu_lim"]),
                _bars("memory", use_mem, values["cur_mem"], values["rec_mem"], values["cur_mem_lim"], values["rec_mem_lim"]),
            ],
        })
    titles = {c["value"]: c["title"] for c in categories}
    recommended = ((pod.get("recommended") or {}).get("values") or {}).get("scheduling") or {}
    return rows, pod_totals, seen_usage, [titles.get(c, c) for c in recommended.get("categories") or []]


def _workload_context(request, resource, reader, cluster):
    ns, kind, name = _workload_args(request)
    if not ns:
        params = _table_params(request, WORKLOAD_SORTS)
        filter_ns = _arg(request, "ns", NAMESPACE_RE)
        if getattr(request, "sfx_scoped", False):
            # A namespace resource: its own workloads, and no list of the cluster's namespaces.
            return {"picker": True, "table_ctx": _workload_table(reader, cluster, params, filter_ns),
                    "app_link": _app_link(reader, cluster, filter_ns)}
        reader.token()
        results = _parallel(
            table=lambda: _workload_table(reader, cluster, params, filter_ns),
            namespaces=lambda: reader.get(f"management/clusters/{_q(cluster)}", {"limit": 100},
                                          allow_404=True, what="the cluster's namespaces"),
        )
        namespaces = sorted(
            d.get("namespace_name") for d in ((results["namespaces"] or {}).get("namespaces") or {}).get("results") or []
            if d.get("namespace_name")
        )
        table = results["table"]
        table["namespaces"] = namespaces
        table["filterable"] = True
        return {"picker": True, "table_ctx": table, "app_link": _app_link(reader, cluster, "namespaces")}

    wid = "_".join((cluster, ns, kind, name))
    v3 = f"v3/workloads/{_q(cluster)}/{_q(ns)}/{_q(kind)}/{_q(name)}"
    reader.token()
    results = _parallel(
        detail=lambda: reader.get(f"management/workloads/{_q(wid)}", allow_404=True, what=f"workload {name}"),
        config=lambda: reader.get(f"management/workloads/{_q(wid)}/config", allow_404=True,
                                  what=f"the settings of workload {name}"),
        workload=lambda: reader.get(v3, allow_404=True, what=f"workload {name}"),
        recommendation=lambda: _latest_recommendation(reader, cluster, ns, kind, name),
        cluster_config=lambda: reader.get(f"management/clusters/{_q(cluster)}/config", allow_404=True,
                                          what="the cluster's configuration"),
    )
    detail = results["detail"]
    if detail is None:
        raise PanelError(f"StormForge has no workload {kind}/{name} in namespace {ns} of cluster {cluster}.")
    recommendation = results["recommendation"] or {}
    categories = _config_view(results["cluster_config"])["categories"]
    rows, pod, seen_usage, instance_categories = _containers(detail, recommendation, categories)
    metrics = (((recommendation.get("pods") or [{}])[0].get("observed") or {}).get("metrics") or {})
    replicas = _float(metrics.get("replicas")) or 1.0
    events = detail.get("events") or {}
    status = detail.get("status") or ""
    reason = detail.get("status_reason") or ""
    hpa = detail.get("hpa") or {}
    has_hpa = any((hpa.get(r) or {}).get("currentTargetUtilization") for r in ("cpu", "memory"))
    labels = (results["workload"] or {}).get("labels") or {}
    cur_cost, rec_cost = _float(detail.get("current_cost")), _float(detail.get("recommended_cost"))
    impact_cost = _float(detail.get("impact_cost"))

    def per_pod(total):
        value = _float(total)
        return None if value is None else value / replicas

    resource_rows = [
        {"label": "Cost", "pod": f"{_money(per_pod(cur_cost))} → {_money(per_pod(rec_cost))}", "pod_unit": "/mo",
         "total": f"{_money(cur_cost)} → {_money(rec_cost)}", "total_unit": "/mo",
         "impact": _money(impact_cost, signed=True), "impact_unit": "/mo",
         "pct": _pct(impact_cost, cur_cost), "dir": _direction(impact_cost)},
        {"label": "CPU", "pod": f"{_cores_short(pod['cur_cpu'] or per_pod(detail.get('current_cpu')))} → "
                                f"{_cores_short(pod['rec_cpu'] or per_pod(detail.get('recommended_cpu')))}", "pod_unit": "Cores",
         "total": f"{_cores(detail.get('current_cpu'))} → {_cores(detail.get('recommended_cpu'))}", "total_unit": "Cores",
         "impact": _cores(detail.get("impact_cpu"), signed=True), "impact_unit": "Cores",
         "pct": _pct(detail.get("impact_cpu"), detail.get("current_cpu")), "dir": _direction(detail.get("impact_cpu"))},
        {"label": "Memory", "pod": f"{_mem(pod['cur_mem'] or per_pod(detail.get('current_memory')))} → "
                                   f"{_mem(pod['rec_mem'] or per_pod(detail.get('recommended_memory')))}", "pod_unit": "",
         "total": f"{_mem(detail.get('current_memory'))} → {_mem(detail.get('recommended_memory'))}", "total_unit": "",
         "impact": _mem(detail.get("impact_memory"), signed=True), "impact_unit": "",
         "pct": _pct(detail.get("impact_memory"), detail.get("current_memory")),
         "dir": _direction(detail.get("impact_memory"))},
    ]
    pod_charts = []
    if rows:
        pod_charts = [
            _bars("cpu", pod["use_cpu"] if seen_usage else None, pod["cur_cpu"], pod["rec_cpu"],
                  pod["cur_cpu_lim"] or None, pod["rec_cpu_lim"] or None),
            _bars("memory", pod["use_mem"] if seen_usage else None, pod["cur_mem"], pod["rec_mem"],
                  pod["cur_mem_lim"] or None, pod["rec_mem_lim"] or None),
        ]
    window = ""
    if recommendation.get("timestamp"):
        window = _stamp(recommendation.get("timestamp"))
    return {
        "picker": False,
        "ns": ns,
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind.split(".")[0].rstrip("s").capitalize()),
        "name": name,
        "summary": _summary(detail),
        "potential_label": "Potential Cost Increase" if (impact_cost or 0) > 0 else "Potential Savings",
        "status": status or "--",
        "status_reason": reason.split("-", 1)[1] if "-" in reason else "",
        "status_class": _status_class(reason or status),
        "generated": _relative(events.get("recommendation_generated")),
        "generated_stamp": _stamp(events.get("recommendation_generated")),
        "next": _relative(events.get("next_scheduled_recommendation") or detail.get("next_scheduled_recommendation")),
        "applied": _relative(events.get("recommendation_applied")),
        "learning": _relative(events.get("learning_complete") or detail.get("learning_complete")),
        "created": _stamp(events.get("workload_created")),
        "auto_deploy": bool(detail.get("auto_deploy")),
        "replicas": _trim(replicas, 1),
        "replicas_plural": replicas != 1,
        "has_hpa": has_hpa,
        "gpu": bool(detail.get("gpu")),
        "label_count": len(labels),
        "labels": sorted(labels.items()),
        "throttling": _rate(detail.get("cpu_throttling_rate")),
        "oom": _rate(detail.get("oom_rate")),
        "messages": _messages(detail),
        "resource_rows": resource_rows,
        "pod_charts": pod_charts,
        "has_usage": seen_usage,
        "containers": rows,
        "instance_categories": instance_categories,
        "group": [
            "/".join(filter(None, (m.get("namespace"), m.get("resource"), m.get("name"))))
            for m in detail.get("workload_group") or []
        ],
        "window": window,
        "settings": _settings(results["config"]),
        "apply_url": _apply_url(request, resource, kind, name),
        "app_link": _app_link(reader, cluster, ns, kind, name, "summary"),
        "recommendation_link": _app_link(reader, cluster, ns, kind, name, "recommendations"),
    }


# -- Apply: start the namespace blueprint's apply action as a job -------------
def _apply_action(profile, resource):
    """The Apply StormForge Recommendation action when it is on the resource's
    blueprint and the viewer may run it on this resource, else None."""
    try:
        from cbhooks.models import ResourceAction
        from utilities.permissions import resource_action_permitted
    except ImportError:
        return None
    try:
        for action in ResourceAction.objects.filter(enabled=True, hook__name=APPLY_ACTION):
            blueprints = list(action.blueprints.values_list("id", flat=True))
            if not blueprints or resource.blueprint_id not in blueprints:
                continue
            if resource_action_permitted(profile, resource, action):
                return action
    except Exception:  # noqa: BLE001 - a missing action only hides the button
        logger.exception("Could not resolve the '%s' action for resource %s", APPLY_ACTION, resource.id)
    return None


def _apply_url(request, resource, kind, name):
    """The Apply dialog for a workload of a namespace resource, or '' when the viewer may not apply."""
    if not getattr(request, "sfx_scoped", False):
        return ""
    if _apply_action(request.get_user_profile(), resource) is None:
        return ""
    return reverse("sfx_apply", args=[resource.id]) + "?" + urllib.parse.urlencode({"type": kind, "name": name})


class ApplyForm(C2Form):
    mode = forms.ChoiceField(
        label="What to do",
        choices=APPLY_MODES,
        initial=APPLY_MODES[0][0],
        help_text="Automatic deployment lets StormForge apply every new recommendation once the "
                  "workload's learning period is over.",
    )


def _dialog_message(title, text):
    return {"title": title, "content": format_html('<p class="text-danger">{}</p>', text)}


def _run_action(request, profile, resource, action, values, what):
    """Start the resource action as a job (or an order when it requires
    approval), the path CloudBolt's own action dialog takes, and report the
    outcome as a page message."""
    try:
        context = get_hook_action_input_dict(action.hook, values, input_names_clean=True)
        result = action.run_hook_as_job(owner=profile, resources=[resource], context=context)
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s failed to start on resource %s", what, resource.id)
        messages.error(request, f"{what} could not be started: {exc}")
        return
    if isinstance(result, (list, tuple)):
        jobs = [job for job in result if hasattr(job, "get_absolute_url")]
        if jobs:
            links = format_html_join(
                ", ", '<a href="{}">Job {}</a>', ((job.get_absolute_url(), job.id) for job in jobs)
            )
            messages.info(request, format_html("{} started: {}.", what, links))
        else:
            messages.error(request, f"{what} did not start a job; check the action's configuration.")
        return
    order = getattr(result, "order", None)
    if order is not None:
        messages.info(request, format_html(
            '{} was added to <a href="{}">Order {}</a>, which needs approval before it runs.',
            what, order.get_absolute_url(), order.id,
        ))
    else:
        messages.info(request, f"{what} was submitted.")


@dialog_view
def sfx_apply(request, resource_id):
    """Apply a workload's latest recommendation, or switch its automatic
    deployment, through the Apply StormForge Recommendation action."""
    title = "Apply StormForge recommendation"
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    if not _can_view(profile, resource):
        return _dialog_message(title, "You do not have permission to view this resource.")
    scope = _scope(resource)
    if not (scope.ns and scope.cluster):
        return _dialog_message(
            title, "Recommendations are applied from a namespace whose cluster runs the StormForge agent.")
    kind = _arg(request, "type", OBJECT_RE)
    name = _arg(request, "name", OBJECT_RE)
    if not (kind and name):
        return _dialog_message(title, "Choose a workload first.")
    action = _apply_action(profile, resource)
    if action is None:
        return _dialog_message(
            title,
            f"The {APPLY_ACTION} action is not available on this namespace: it is not synced, not on the "
            "namespace's blueprint, or you are not allowed to run it.",
        )
    # The workload must be one StormForge reports in this resource's namespace.
    try:
        detail = Reader().get(
            f"management/workloads/{_q('_'.join((scope.cluster, scope.ns, kind, name)))}",
            allow_404=True, what=f"workload {name}")
    except PanelError as exc:
        return _dialog_message(title, str(exc))
    if detail is None:
        return _dialog_message(title, f"StormForge has no workload {kind}/{name} in namespace {scope.ns}.")

    if request.method == "POST":
        form = ApplyForm(request.POST)
        if form.is_valid():
            mode = form.cleaned_data["mode"]
            _run_action(
                request, profile, resource, action,
                {"stormforge_workload": f"{kind}/{name}", "stormforge_apply_mode": mode},
                f"{dict(APPLY_MODES)[mode]} for {name}",
            )
            return HttpResponseRedirect(request.META.get("HTTP_REFERER") or resource.get_absolute_url())
    else:
        form = ApplyForm()

    row = _row(detail)
    content = format_html(
        "<p><b>{}</b> ({}) in namespace <b>{}</b>. StormForge recommends CPU requests {} &rarr; {} cores "
        "and memory requests {} &rarr; {}, a net {} per month. Automatic deployment is {}.</p>"
        "<p>Applying changes the workload's requests and limits, and Kubernetes rolls its pods to the new "
        "values. The change runs as a CloudBolt job through the <b>{}</b> action.</p>",
        name, KIND_LABELS.get(kind, kind), scope.ns,
        row["cur_cpu"], row["rec_cpu"], row["cur_mem"], row["rec_mem"], row["imp_cost"],
        "on" if detail.get("auto_deploy") else "off", action.label,
    )
    return {
        "title": title,
        "content": content,
        "form": form,
        "action_url": reverse("sfx_apply", args=[resource.id]) + "?"
                      + urllib.parse.urlencode({"type": kind, "name": name}),
        "use_ajax": True,
        "submit": "Run",
    }


def _patches_context(request, resource, reader, cluster):
    ns, kind, name = _workload_args(request)
    if not ns:
        raise PanelError("Choose a workload first.")
    preview = request.GET.get("preview") == "1"
    wid = "_".join((cluster, ns, kind, name))
    data = reader.get(f"management/workloads/{_q(wid)}/patches",
                      {"node-affinity-preview": "true" if preview else ""}, allow_404=True,
                      what=f"the patches of workload {name}") or {}
    patches = [
        {"title": p.get("title") or "", "filename": p.get("filename") or "patch.yaml",
         "command": p.get("command") or "", "data": p.get("data") or ""}
        for p in data.get("patches") or []
    ]
    return {
        "ns": ns, "kind": kind, "name": name,
        "patches": patches,
        "preview": preview,
        "preview_available": bool(data.get("nodeAffinityPreviewAvailable")),
    }


# ---------------------------------------------------------------------------
# Panels (HTML fragments the tabs load asynchronously)
# ---------------------------------------------------------------------------
def _panel(request, resource_id, template, build, cluster_only=False):
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    scope = _scope(resource)
    cluster = scope.cluster
    request.sfx_scoped = bool(scope.ns)
    context = {}
    if not _can_view(profile, resource):
        context = {"error": "You do not have permission to view this resource."}
    elif not cluster and scope.ns:
        context = {"error": "This namespace's cluster does not run the StormForge agent."}
    elif not cluster:
        context = {"error": "This resource has no StormForge cluster name; install the StormForge agent first."}
    elif scope.ns and cluster_only:
        context = {"error": "This view belongs to the cluster, not to a namespace."}
    else:
        if scope.ns:
            # A namespace resource shows its own namespace, whatever the request names.
            query = request.GET.copy()
            query["ns"] = scope.ns
            request.GET = query
        try:
            reader = Reader(fresh=request.GET.get("fresh") == "1")
            context = build(request, resource, reader, cluster)
        except PanelError as exc:
            context = {"error": str(exc)}
        except Exception:  # noqa: BLE001
            logger.exception("StormForge panel %s failed for resource %s", template, resource.id)
            context = {"error": "Could not load this panel. The CloudBolt application log has the details."}
    context.update(resource=resource, cluster=cluster, icon=ICON, scoped=bool(scope.ns))
    if scope.ns:
        context.setdefault("ns", scope.ns)
    return render(request, TEMPLATES + template, context)


def sfx_cluster_panel(request, resource_id):
    return _panel(request, resource_id, "sfx_cluster_panel.html", _cluster_context, cluster_only=True)


def sfx_nodes_panel(request, resource_id):
    return _panel(request, resource_id, "sfx_nodes_panel.html", _nodes_context, cluster_only=True)


def sfx_namespace_panel(request, resource_id):
    return _panel(request, resource_id, "sfx_namespace_panel.html", _namespace_context)


def sfx_workload_panel(request, resource_id):
    return _panel(request, resource_id, "sfx_workload_panel.html", _workload_context)


def sfx_patches_panel(request, resource_id):
    return _panel(request, resource_id, "sfx_patches_panel.html", _patches_context)


def sfx_agent_panel(request, resource_id):
    return _panel(request, resource_id, "sfx_agent_panel.html", _agent_context, cluster_only=True)


def sfx_agent_log(request, resource_id):
    return _panel(request, resource_id, "sfx_agent_log.html", _agent_log_context, cluster_only=True)


def sfx_namespaces_table(request, resource_id):
    def build(request, resource, reader, cluster):
        _detail, table = _namespace_table(reader, cluster, _table_params(request, NAMESPACE_SORTS))
        return {"table_ctx": table}
    return _panel(request, resource_id, "sfx_table.html", build, cluster_only=True)


def sfx_workloads_table(request, resource_id):
    def build(request, resource, reader, cluster):
        ns = _arg(request, "ns", NAMESPACE_RE)
        table = _workload_table(reader, cluster, _table_params(request, WORKLOAD_SORTS), ns)
        table["filterable"] = request.GET.get("filterable") == "1" and not getattr(request, "sfx_scoped", False)
        return {"table_ctx": table}
    return _panel(request, resource_id, "sfx_table.html", build)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
class StormForgeTabDelegate(TabExtensionDelegate):
    """Kubernetes clusters with the StormForge agent installed.

    The agent install records the StormForge cluster name before Helm runs and
    the agent version only after Helm succeeds, so a failed first install
    leaves the tabs hidden.
    """

    def should_display(self):
        return _agent_installed(self.instance) and bool(_cluster_name(self.instance))


class StormForgeNamespaceTabDelegate(TabExtensionDelegate):
    """EKS, AKS and GKE namespaces whose cluster runs the StormForge agent, on
    a CloudBolt that has the StormForge connection info."""

    def should_display(self):
        try:
            scope = _scope(self.instance)
            if not (scope.ns and scope.cluster):
                return False
            return ConnectionInfo.objects.filter(name=CONNECTION_INFO).exists()
        except Exception:  # noqa: BLE001 - never break the resource page
            logger.exception("StormForge namespace tab check failed for resource %s", self.instance.id)
            return False


def _tab(request, obj_id, view):
    resource = get_object_or_404(Resource, pk=obj_id)
    title, label = TABS[view]
    # A namespace's tab holds the namespace view and the workload view.
    views = ["namespace", "workload"] if view == "scoped" else [view]
    return render(request, TEMPLATES + "sfx_tab.html", {
        "resource": resource,
        "view": views[0],
        "views": views,
        "label": label,
        "tab_id": "tab-" + title.lower().replace(" ", "-"),
        "root_id": f"sfx-{view}-{resource.id}",
        "icon": ICON,
        "urls": {key: reverse(name, args=[resource.id]) for key, name in ENDPOINTS.items()},
    })


@tab_extension(
    model=Resource,
    title=TABS["cluster"][0],
    delegate=StormForgeTabDelegate,
    description="StormForge optimization score, recommendation impact, namespaces, nodes, and configuration for the cluster",
)
def stormforge_clusters_tab(request, obj_id):
    return _tab(request, obj_id, "cluster")


@tab_extension(
    model=Resource,
    title=TABS["namespace"][0],
    delegate=StormForgeTabDelegate,
    description="StormForge recommendations for a namespace of the cluster and its workloads",
)
def stormforge_namespaces_tab(request, obj_id):
    return _tab(request, obj_id, "namespace")


@tab_extension(
    model=Resource,
    title=TABS["workload"][0],
    delegate=StormForgeTabDelegate,
    description="StormForge workload summary, recommendation, patches, and optimization settings",
)
def stormforge_workloads_tab(request, obj_id):
    return _tab(request, obj_id, "workload")


@tab_extension(
    model=Resource,
    title=TABS["scoped"][0],
    delegate=StormForgeNamespaceTabDelegate,
    description="StormForge optimization score, recommendation impact, workloads and recommendations for the namespace",
)
def stormforge_namespace_tab(request, obj_id):
    return _tab(request, obj_id, "scoped")
