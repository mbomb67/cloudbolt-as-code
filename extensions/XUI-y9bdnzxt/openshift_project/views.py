"""
OpenShift Project XUI

Adds Connect and Workloads tabs to the OpenShift Project resources of the
OpenShift Project Landing Zone blueprint (BP-tikkhf2y): openshift_project
resources carrying openshift_project_name. They show what the namespace tabs
of the AWS EKS (XUI-8a1jocmr), Azure AKS (XUI-nr5iiu60), and Google GKE
(XUI-8egdz3e9) extensions show, in OpenShift's terms, plus the project's
OpenShift Virtualization virtual machines and its Routes.

- Connect: where the project lives (cluster, API server, console, CloudBolt
  environment, size tier, network isolation, expiration), the OpenShift
  users, groups, and service accounts a RoleBinding in the project names (and
  the cluster admins), the CloudBolt groups that may order into it, and the oc
  commands that sign in and switch to it; Manage access (Manage Team Access).
- Workloads: a read-only inventory of the project: the alerts the cluster's
  monitoring is firing for it and the object each is about, usage of
  CloudBolt's quota (and any other quota object in it), virtual machines,
  Deployments, StatefulSets and DaemonSets, pods, services, Routes and
  ingresses, persistent volume claims, and recent warning events; Request
  quota change (Request Quota Change). No logs, Secrets or ConfigMaps, and no
  buttons that change the cluster.

Every button opens CloudBolt's own dialog for one of the blueprint's resource
actions, so approvals, permissions and job history apply. Buttons appear only
when the action is synced, applies to the resource's blueprint and the viewer
may run it (resource_action_permitted), and the job runs as the viewer.

OpenShift is read through the openshift_landing_zone shared module
(SHM-qmiweowv) with the credentials of the OpenShift Virtualization resource
handler that owns the project (openshift_project_rh_id); the handler is never
offered to the viewer. The module is imported inside each view, so a
CloudBolt without it shows an alert instead of failing to load the extension.
Reads run as the handler's identity, not the viewer's, so every endpoint
re-checks the viewer's CloudBolt permission on the resource. Panels load
asynchronously so a slow cluster API never stalls the page.

Vendor API references (each call site cites its own page):
    https://kubernetes.io/docs/reference/kubernetes-api/
    https://docs.redhat.com/en/documentation/openshift_container_platform/4.16/html/network_apis/route-route-openshift-io-v1
    https://docs.redhat.com/en/documentation/openshift_container_platform/4.16/html/cli_tools/openshift-cli-oc
    https://github.com/kubevirt/api/blob/main/core/v1/types.go
    https://prometheus.io/docs/prometheus/latest/querying/api/#alerts
"""

import datetime
import importlib
import inspect
import re
import threading
from urllib.parse import urlparse

from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from cbhooks.models import ResourceAction
from extensions.views import tab_extension, TabExtensionDelegate
from infrastructure.models import Server
from resources.models import Resource
from utilities.logger import ThreadLogger
from utilities.permissions import resource_action_permitted

logger = ThreadLogger(__name__)

TEMPLATES = "openshift_project/templates/"

# The openshift_landing_zone shared module (SHM-qmiweowv), imported lazily by _lz().
SHARED_MODULE = "shared_modules.openshift_landing_zone"

# Viewing the tabs needs VIEW_PERMISSION on the resource. Every button maps
# to a resource action and is shown only when resource_action_permitted
# allows that action for the viewer.
VIEW_PERMISSION = "resource.view"

# Resource actions of the OpenShift Project Landing Zone blueprint as
# (global id, label): matched by global id, else by label.
ACTION_MANAGE_ACCESS = ("RSA-yj1c4b5s", "Manage Team Access")
ACTION_REQUEST_QUOTA_CHANGE = ("RSA-4e8lmj2r", "Request Quota Change")

# The resources the tabs belong on, and the custom fields the blueprint
# stores on them (openshift_landing_zone.CF_PREFIX).
RESOURCE_TYPE = "openshift_project"
CF_PREFIX = "openshift_project_"
MARKER_FIELD = CF_PREFIX + "name"


class PanelError(Exception):
    """A readable problem the panel shows instead of a traceback."""


# ---------------------------------------------------------------------------
# Shared module, resource and permission helpers
# ---------------------------------------------------------------------------
def _lz():
    """The openshift_landing_zone shared module, or PanelError when it is not synced."""
    try:
        return importlib.import_module(SHARED_MODULE)
    except ImportError:
        raise PanelError("The openshift_landing_zone shared module (SHM-qmiweowv) is not on this CloudBolt.")


def _is_lz_error(exc):
    try:
        return isinstance(exc, _lz().LandingZoneError)
    except PanelError:
        return False


def _attr(resource, name):
    """Value of a custom field on the resource (exposed as an attribute), or None."""
    try:
        value = getattr(resource, name)
    except AttributeError:
        return None
    if value in ("", None):
        return None
    return value


HREF_RE = re.compile(r'href="([^"]+)"')


def _url(value):
    """An https URL from a URL custom field, which CloudBolt may render as an
    <a href> tag; '' for anything else."""
    text = str(value or "").strip()
    match = HREF_RE.search(text)
    if match:
        text = match.group(1)
    return text if text.startswith("https://") else ""


def _has_permission(profile, permission, resource):
    if getattr(profile, "is_cbadmin", False):
        return True
    try:
        return bool(profile.has_permission(permission, resource))
    except Exception:
        logger.exception("Permission check '%s' failed for resource %s", permission, resource.id)
        return False


def _can_view(profile, resource):
    return _has_permission(profile, VIEW_PERMISSION, resource)


def _action_for(profile, resource, action):
    """The enabled resource action with the global id (or label) of ``action``,
    when it applies to the resource's blueprint and the viewer may run it on
    this resource (the checks the resource page's action sidebar applies),
    else None."""
    global_id, label = action
    try:
        candidates = ResourceAction.objects.filter(enabled=True).filter(Q(global_id=global_id) | Q(label=label))
        for candidate in sorted(candidates.distinct(), key=lambda a: a.global_id != global_id):
            blueprint_ids = list(candidate.blueprints.values_list("id", flat=True))
            if blueprint_ids and resource.blueprint_id not in blueprint_ids:
                continue
            if not resource_action_permitted(profile, resource, candidate):
                continue
            return candidate
    except Exception:
        logger.exception("Could not resolve the '%s' action for resource %s", label, resource.id)
    return None


def _action_url(profile, resource, action):
    """CloudBolt's own run dialog for the action, or '' when unavailable."""
    found = _action_for(profile, resource, action)
    if found is None:
        return ""
    return reverse("run_resource_action", args=[resource.id, found.id])


def _client(resource):
    """(namespace, OpenShiftLandingZoneClient) signed in as the project's
    resource handler, or PanelError."""
    lz = _lz()
    try:
        namespace, handler = lz.load_handler(resource)
    except ValueError as exc:
        raise PanelError("This project cannot be read: {}.".format(exc))
    if not lz.NAMESPACE_NAME_RE.match(str(namespace)):
        raise PanelError("'{}' is not a valid OpenShift project name.".format(namespace))
    try:
        return namespace, lz.OpenShiftLandingZoneClient.from_handler(handler)
    except lz.LandingZoneError as exc:
        raise PanelError(str(exc))
    except Exception as exc:
        # The handler's API wrapper signs in when it connects and raises a
        # plain Exception when the cluster refuses its credentials.
        logger.warning("Signing in to OpenShift for resource %s failed: %s", resource.id, exc)
        raise PanelError("Could not sign in to OpenShift cluster {}: {}".format(
            _attr(resource, CF_PREFIX + "cluster") or "", str(exc)[:300]))


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _pill(text, style, icon, spin=False):
    return {"text": text, "style": style, "icon": icon, "spin": spin}


def _age(iso_timestamp):
    """Compact age of a Kubernetes object from its creationTimestamp."""
    if not iso_timestamp:
        return ""
    try:
        created = datetime.datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return ""
    delta = datetime.datetime.now(datetime.timezone.utc) - created
    if delta.days >= 1:
        return "{}d".format(delta.days)
    hours = delta.seconds // 3600
    if hours:
        return "{}h".format(hours)
    return "{}m".format(delta.seconds // 60)


CPU_SUFFIXES = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
MEMORY_SUFFIXES = {
    "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
    "k": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4, "P": 1000 ** 5, "E": 1000 ** 6,
}
QUANTITY_RE = re.compile(r"^([0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)([A-Za-z]*)$")


def _cpu_cores(quantity):
    """Kubernetes CPU quantity (500m, 2, 1500000n) as cores."""
    match = QUANTITY_RE.match(str(quantity or "").strip())
    if not match:
        return 0.0
    number, suffix = match.groups()
    return float(number) * CPU_SUFFIXES.get(suffix, 1.0)


def _memory_bytes(quantity):
    """Kubernetes memory quantity (128Mi, 2Gi, 1000000, 1e9) as bytes."""
    match = QUANTITY_RE.match(str(quantity or "").strip())
    if not match:
        return 0.0
    number, suffix = match.groups()
    return float(number) * MEMORY_SUFFIXES.get(suffix, 1)


def _cores_text(cores):
    return "{:.2f}".format(cores).rstrip("0").rstrip(".") or "0"


def _memory_text(size):
    if size >= 1024 ** 3:
        return "{:.1f} GiB".format(size / 1024 ** 3)
    return "{:.0f} MiB".format(size / 1024 ** 2)


def _meter(used, total, used_text, total_text):
    percent = int(round(100.0 * used / total)) if total else 0
    style = "ok" if percent < 75 else "warn" if percent < 90 else "bad"
    return {
        "requests": used_text,
        "allocatable": total_text,
        "percent": max(0, min(100, percent)),
        "style": style,
    }


# ---------------------------------------------------------------------------
# Connect tab: how to reach the project, and who may
# ---------------------------------------------------------------------------
# Access to a project is a RoleBinding in its namespace to one of OpenShift's
# default project roles (the ClusterRoles admin, edit, view) for an OpenShift
# user, group, or service account; Manage Team Access creates them with the
# landing-zone label. Users and groups bound to cluster-admin reach every
# project and are listed too. RoleBindings named system:, which OpenShift
# creates in every project for its own service accounts, are left out.
#   https://kubernetes.io/docs/reference/kubernetes-api/authorization-resources/role-binding-v1/
#   https://kubernetes.io/docs/reference/kubernetes-api/authorization-resources/cluster-role-binding-v1/
ROLE_LABELS = {"admin": "Admin", "edit": "Edit", "view": "View", "cluster-admin": "Cluster admin"}
SUBJECT_KINDS = {"User": "User", "Group": "Group", "ServiceAccount": "Service account"}
ACCESS_SCOPES = {"namespace": "This project", "cluster": "Whole cluster"}
RBAC = "/apis/rbac.authorization.k8s.io/v1"


def _access_row(subject, role_ref, scope, managed, binding, namespace):
    kind = subject.get("kind", "")
    name = subject.get("name", "")
    if kind == "ServiceAccount" and subject.get("namespace") not in (None, "", namespace):
        name = "{}/{}".format(subject.get("namespace"), name)
    role = role_ref.get("name", "")
    if role_ref.get("kind") == "Role":
        label = "{} (Role)".format(role)
    else:
        label = ROLE_LABELS.get(role, role)
    return {
        "name": name,
        "type_text": SUBJECT_KINDS.get(kind, kind),
        "label": label,
        "scope": scope,
        "scope_text": ACCESS_SCOPES[scope],
        "managed": managed,
        "binding": binding,
    }


def _project_access(lz, client, namespace):
    """(rows, note): who can work in the project through RBAC, and why the
    cluster admins are missing when they could not be listed."""
    rows = []
    for item in client.list_items("{}/namespaces/{}/rolebindings".format(RBAC, namespace)):
        metadata = item.get("metadata") or {}
        binding = metadata.get("name", "")
        if binding.startswith("system:"):
            continue
        managed = (metadata.get("labels") or {}).get(lz.LANDING_ZONE_LABEL) == "true"
        for subject in item.get("subjects") or []:
            rows.append(_access_row(subject, item.get("roleRef") or {}, "namespace", managed, binding, namespace))

    note = ""
    try:
        cluster_bindings = client.list_items("{}/clusterrolebindings".format(RBAC))
    except lz.LandingZoneError as exc:
        # Listing ClusterRoleBindings needs cluster-wide read on RBAC, which a
        # handler scoped to its projects may not have.
        logger.info("Cluster role bindings not listed for %s: %s", namespace, exc)
        cluster_bindings = []
        note = "Cluster admins could not be listed: {}".format(exc)
    for item in cluster_bindings:
        role_ref = item.get("roleRef") or {}
        if role_ref.get("name") != "cluster-admin":
            continue
        for subject in item.get("subjects") or []:
            if subject.get("kind") not in ("User", "Group") or subject.get("name", "").startswith("system:"):
                continue
            rows.append(_access_row(subject, role_ref, "cluster", False,
                                    (item.get("metadata") or {}).get("name", ""), namespace))
    rows.sort(key=lambda r: (r["scope"] != "namespace", r["name"].lower(), r["label"]))
    return rows, note


def _network_isolation(lz, client, namespace):
    """The project's NetworkPolicies, described: CloudBolt's multitenant
    isolation set, other policies, or none.
    https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/network-policy-v1/"""
    names = sorted(
        (item.get("metadata") or {}).get("name", "")
        for item in client.list_items("/apis/networking.k8s.io/v1/namespaces/{}/networkpolicies".format(namespace))
    )
    ours = {policy["name"] for policy in lz.OpenShiftLandingZoneClient.NETWORK_POLICIES}
    if ours.issubset(names):
        text = "Multitenant isolation"
    elif names:
        text = "{} NetworkPolic{}".format(len(names), "y" if len(names) == 1 else "ies")
    else:
        text = "None: no NetworkPolicies"
    return {"text": text, "policies": names}


def _cloudbolt_groups(lz, resource):
    """(state, names) for the CloudBolt groups that may order into the
    project's environment: 'entitled' with their names; 'unassigned' when
    only CloudBolt's Unassigned placeholder holds it (no group may order);
    'unconstrained' when no group does (every group may); '' without an
    environment."""
    from accounts.models import Group

    env = lz.load_environment(resource)
    if env is None:
        return "", []
    names = lz.entitled_group_names(env)
    if names:
        return "entitled", names
    if Group.objects.filter(environments=env).exists():
        return "unassigned", []
    return "unconstrained", []


def _commands(api_url, namespace):
    """The shell commands that sign in to the cluster and switch to the project.
    oc login --web, oc project:
      https://docs.redhat.com/en/documentation/openshift_container_platform/4.16/html/cli_tools/openshift-cli-oc
    """
    return [
        "oc login {} --web".format(api_url),
        "oc project {}".format(namespace),
        "oc get pods",
        "oc get virtualmachines",
    ]


def _connect_context(resource, profile):
    lz = _lz()
    namespace, client = _client(resource)
    try:
        # https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/namespace-v1/
        project = client.get_namespace(namespace)
        if project is None:
            raise PanelError("Project {} no longer exists on the cluster.".format(namespace))
        try:
            access, access_note = _project_access(lz, client, namespace)
            access_error = ""
        except lz.LandingZoneError as exc:
            access, access_note, access_error = [], "", "RoleBindings could not be read: {}".format(exc)
        try:
            isolation = _network_isolation(lz, client, namespace)
        except lz.LandingZoneError as exc:
            isolation = {"text": "Unknown: {}".format(exc), "policies": []}
        console_url = _url(_attr(resource, CF_PREFIX + "console_url")) or client.project_console_url(namespace)
        api_url = client.api_url
    finally:
        client.close()

    metadata = project.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    labels = metadata.get("labels") or {}
    phase = (project.get("status") or {}).get("phase", "")
    tier_key = _attr(resource, CF_PREFIX + "size_tier") or labels.get(lz.LABEL_TIER, "")
    tier = lz.TIERS.get(str(tier_key).lower())
    expires = lz.resource_expiration(resource)
    groups_state, groups = _cloudbolt_groups(lz, resource)
    return {
        "namespace": namespace,
        "status": _pill("Active", "ok", "bi-check-circle") if phase == "Active"
        else _pill(phase or "Unknown", "warn", "bi-hourglass-split"),
        "display_name": annotations.get(lz.ANNOTATION_OS_DISPLAY_NAME) or "",
        "description": annotations.get(lz.ANNOTATION_OS_DESCRIPTION) or "",
        "requester": annotations.get(lz.ANNOTATION_OS_REQUESTER) or annotations.get(lz.ANNOTATION_OWNER) or "",
        "created": (metadata.get("creationTimestamp") or "")[:10],
        "cluster_name": _attr(resource, CF_PREFIX + "cluster") or "",
        "api_url": api_url,
        "console_url": console_url,
        "environment_name": _attr(resource, CF_PREFIX + "environment_name") or "",
        "owner_group": resource.group.name if resource.group_id else "",
        "tier": "{} ({})".format(tier["label"], tier["description"]) if tier else tier_key,
        "isolation": isolation,
        "expires": expires.isoformat() if expires else "",
        "expired": bool(expires and expires < datetime.date.today()),
        "access": access,
        "access_note": access_note,
        "access_error": access_error,
        "groups_state": groups_state,
        "groups": groups,
        "commands": _commands(api_url, namespace),
    }


# ---------------------------------------------------------------------------
# Workloads tab: what runs in the project
# ---------------------------------------------------------------------------
# Read-only, with the handler's credentials. Deliberately no pod logs,
# Secrets or ConfigMaps (anyone who may view the project resource would see
# them), and no start, stop, scale or restart buttons (they would run as the
# handler, not as the viewer).
#   ResourceQuota  https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/resource-quota-v1/
#   VirtualMachine https://github.com/kubevirt/api/blob/main/core/v1/types.go
#   Deployment     https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/
#   StatefulSet    https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/stateful-set-v1/
#   DaemonSet      https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/daemon-set-v1/
#   Pod            https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/
#   Service        https://kubernetes.io/docs/reference/kubernetes-api/service-resources/service-v1/
#   Route          https://docs.redhat.com/en/documentation/openshift_container_platform/4.16/html/network_apis/route-route-openshift-io-v1
#   Ingress        https://kubernetes.io/docs/reference/kubernetes-api/service-resources/ingress-v1/
#   PVC            https://kubernetes.io/docs/reference/kubernetes-api/config-and-storage-resources/persistent-volume-claim-v1/
#   Event          https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/event-v1/

# ResourceQuota keys in display order: label, how to read the quantity, and
# the unit shown after a count in the meter's hover text. CloudBolt's quota
# (openshift_landing_zone.quota_hard_for_tier) carries all of them.
QUOTA_KEYS = [
    ("requests.cpu", "CPU requests", "cpu", ""),
    ("limits.cpu", "CPU limits", "cpu", ""),
    ("requests.memory", "Memory requests", "memory", ""),
    ("limits.memory", "Memory limits", "memory", ""),
    ("requests.storage", "Storage", "memory", ""),
    ("persistentvolumeclaims", "Volume claims", "count", "claims"),
    ("pods", "Pods", "count", "pods"),
    ("count/virtualmachines.kubevirt.io", "Virtual machines", "count", "VMs"),
]
# Warning events shown, newest first. The API server keeps events for an hour
# by default (kube-apiserver --event-ttl):
# https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/
MAX_EVENTS = 25

# VirtualMachine status.printableStatus values (kubevirt.io/v1
# VirtualMachinePrintableStatus) that are a transition rather than a state.
VM_TRANSITIONS = {
    "Provisioning", "Starting", "Stopping", "Terminating", "Migrating",
    "WaitingForVolumeBinding", "WaitingForReceiver",
}

# Route spec.tls.termination values.
TLS_TERMINATIONS = {"edge": "Edge TLS", "passthrough": "Passthrough TLS", "reencrypt": "Re-encrypt TLS"}


def _quantity(kind, value):
    if kind == "cpu":
        return _cpu_cores(value)
    if kind == "memory":
        return _memory_bytes(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _quantity_text(kind, amount):
    if kind == "cpu":
        return "{} cores".format(_cores_text(amount))
    if kind == "memory":
        return _memory_text(amount)
    return "{:.0f}".format(amount)


def _usage_text(kind, used, total, unit):
    """Hover text for a quota meter: '0.3 / 4 cores', '384 MiB / 16.0 GiB', '3 / 20 pods'."""
    if kind == "cpu":
        return "{} / {} cores".format(_cores_text(used), _cores_text(total))
    if kind == "memory":
        return "{} / {}".format(_memory_text(used), _memory_text(total))
    return "{:.0f} / {:.0f}{}".format(used, total, " " + unit if unit else "")


def _quota_usage(quotas):
    """{quota name: {resource: (hard, used)}} from the namespace's ResourceQuotas."""
    usage = {}
    for item in quotas:
        status = item.get("status") or {}
        hard = status.get("hard") or (item.get("spec") or {}).get("hard") or {}
        used = status.get("used") or {}
        usage[(item.get("metadata") or {}).get("name", "")] = {k: (v, used.get(k, "0")) for k, v in hard.items()}
    return usage


def _quota_rows(usage, cloudbolt_quota):
    """One meter per hard limit of CloudBolt's ResourceQuota, and every other
    quota object in the project as plain rows. Returns (meters, other_quotas)."""
    known = [key for key, _, _, _ in QUOTA_KEYS]
    meters, others = [], []
    for quota_name in sorted(usage):
        limits = usage[quota_name] or {}
        if quota_name != cloudbolt_quota:
            others.append({
                "name": quota_name,
                "limits": [{"key": key, "hard": limits[key][0], "used": limits[key][1]} for key in sorted(limits)],
            })
            continue
        ordered = [entry for entry in QUOTA_KEYS if entry[0] in limits]
        ordered += [(k, k, "count", "") for k in sorted(limits) if k not in known]
        for key, label, kind, unit in ordered:
            hard, used = limits[key]
            total = _quantity(kind, hard)
            current = _quantity(kind, used)
            meter = _meter(current, total, _quantity_text(kind, current), _quantity_text(kind, total))
            meter.update({"label": label, "usage": _usage_text(kind, current, total, unit), "quota": quota_name})
            meters.append(meter)
    return meters, others


def _vm_row(vm, instance, server_url):
    """A VirtualMachine with the address and node of its running instance."""
    metadata, status = vm.get("metadata") or {}, vm.get("status") or {}
    text = status.get("printableStatus") or "Unknown"
    if text == "Running":
        pill = _pill(text, "ok", "bi-check-circle-fill")
    elif text == "Stopped":
        pill = _pill(text, "muted", "bi-stop-circle")
    elif text == "Paused":
        pill = _pill(text, "warn", "bi-pause-circle-fill")
    elif text in VM_TRANSITIONS:
        pill = _pill(text, "live", "bi-arrow-repeat", spin=True)
    else:
        pill = _pill(text, "bad", "bi-x-circle-fill")
    instance_status = (instance or {}).get("status") or {}
    addresses = [i.get("ipAddress") for i in instance_status.get("interfaces") or [] if i.get("ipAddress")]
    return {
        "name": metadata.get("name", ""),
        "status": pill,
        "running": text == "Running",
        "ready": bool(status.get("ready")),
        "address": ", ".join(addresses),
        "node": instance_status.get("nodeName", ""),
        "age": _age(metadata.get("creationTimestamp")),
        "server_url": server_url,
    }


def _server_urls(resource, names):
    """{VM name: CloudBolt server page} for the project environment's servers;
    OpenShift Virtualization servers are named after their VirtualMachine."""
    env_id = _attr(resource, CF_PREFIX + "environment_id")
    if not env_id or not names:
        return {}
    try:
        servers = Server.objects.filter(environment_id=int(env_id), hostname__in=names).exclude(status="HISTORICAL")
        return {server.hostname: server.get_absolute_url() for server in servers}
    except Exception:
        logger.exception("Could not match virtual machines to servers for resource %s", resource.id)
        return {}


def _images(pod_spec):
    return sorted({c.get("image", "") for c in (pod_spec or {}).get("containers", []) or [] if c.get("image")})


def _workload_row(kind, item):
    metadata, spec, status = item.get("metadata") or {}, item.get("spec") or {}, item.get("status") or {}
    if kind == "DaemonSet":
        wanted, ready = status.get("desiredNumberScheduled") or 0, status.get("numberReady") or 0
    else:
        wanted = spec.get("replicas") if spec.get("replicas") is not None else 1
        ready = status.get("readyReplicas") or 0
    if wanted == 0:
        pill = _pill("Scaled to 0", "muted", "bi-pause-circle")
    elif ready >= wanted:
        pill = _pill("Ready", "ok", "bi-check-circle-fill")
    elif ready:
        pill = _pill("Degraded", "warn", "bi-exclamation-triangle-fill")
    else:
        pill = _pill("Unavailable", "bad", "bi-x-circle-fill")
    return {
        "kind": kind,
        "name": metadata.get("name", ""),
        "ready": "{}/{}".format(ready, wanted),
        "status": pill,
        "images": _images((spec.get("template") or {}).get("spec")),
        "age": _age(metadata.get("creationTimestamp")),
    }


def _pod_row(pod):
    metadata, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
    containers = status.get("containerStatuses") or []
    reason = status.get("reason") or ""
    for container in containers:
        state = container.get("state") or {}
        waiting = (state.get("waiting") or {}).get("reason")
        terminated = (state.get("terminated") or {}).get("reason")
        if waiting or (terminated and terminated != "Completed"):
            reason = waiting or terminated
            break
    phase = status.get("phase", "Unknown")
    if not reason and phase == "Pending":
        for condition in status.get("conditions") or []:
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                reason = condition.get("reason") or ""
    text = reason or phase
    if phase == "Running" and not reason:
        pill = _pill(text, "ok", "bi-check-circle-fill")
    elif phase == "Succeeded":
        pill = _pill(text, "muted", "bi-check-circle")
    elif phase == "Pending" and not reason:
        pill = _pill(text, "live", "bi-hourglass-split")
    else:
        pill = _pill(text, "bad", "bi-x-circle-fill")
    owners = metadata.get("ownerReferences") or []
    return {
        "name": metadata.get("name", ""),
        "status": pill,
        "running": phase == "Running" and not reason,
        "ready": "{}/{}".format(sum(1 for c in containers if c.get("ready")), len(spec.get("containers") or [])),
        "restarts": sum(c.get("restartCount") or 0 for c in containers),
        "node": spec.get("nodeName", ""),
        "owner": "{} {}".format(owners[0].get("kind", ""), owners[0].get("name", "")) if owners else "",
        "age": _age(metadata.get("creationTimestamp")),
    }


def _load_balancer_addresses(item):
    return ", ".join(
        entry.get("hostname") or entry.get("ip") or ""
        for entry in ((item.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        if entry.get("hostname") or entry.get("ip")
    )


def _service_row(service):
    metadata, spec = service.get("metadata") or {}, service.get("spec") or {}
    ports = []
    for port in spec.get("ports") or []:
        text = "{}/{}".format(port.get("port"), port.get("protocol", "TCP"))
        if port.get("targetPort") not in (None, port.get("port")):
            text += " → {}".format(port.get("targetPort"))
        ports.append(text)
    return {
        "kind": "Service",
        "name": metadata.get("name", ""),
        "type": spec.get("type", ""),
        "address": spec.get("clusterIP", ""),
        "ports": ", ".join(ports),
        "external": _load_balancer_addresses(service),
        "age": _age(metadata.get("creationTimestamp")),
    }


def _route_row(route):
    """A Route with its host, TLS termination, target service, and whether a
    router admitted it."""
    metadata, spec, status = route.get("metadata") or {}, route.get("spec") or {}, route.get("status") or {}
    ingress = status.get("ingress") or []
    host = spec.get("host") or next((i.get("host") for i in ingress if i.get("host")), "")
    path = spec.get("path") or ""
    termination = (spec.get("tls") or {}).get("termination") or ""
    admitted = any(
        c.get("type") == "Admitted" and c.get("status") == "True"
        for i in ingress for c in i.get("conditions") or []
    )
    target = (spec.get("to") or {}).get("name", "")
    port = (spec.get("port") or {}).get("targetPort")
    return {
        "kind": "Route",
        "name": metadata.get("name", ""),
        "type": TLS_TERMINATIONS.get(termination, "HTTP"),
        "address": "",
        "ports": host + path,
        "target": "{}{}".format(target, ":{}".format(port) if port not in (None, "") else ""),
        "url": "{}://{}{}".format("https" if termination else "http", host, path) if host and admitted else "",
        "admitted": admitted,
        "external": "",
        "age": _age(metadata.get("creationTimestamp")),
    }


def _ingress_row(ingress):
    metadata, spec = ingress.get("metadata") or {}, ingress.get("spec") or {}
    hosts = [rule.get("host") for rule in spec.get("rules") or [] if rule.get("host")]
    return {
        "kind": "Ingress",
        "name": metadata.get("name", ""),
        "type": spec.get("ingressClassName", ""),
        "address": "",
        "ports": ", ".join(hosts) or "*",
        "external": _load_balancer_addresses(ingress),
        "age": _age(metadata.get("creationTimestamp")),
    }


def _claim_row(claim):
    metadata, spec, status = claim.get("metadata") or {}, claim.get("spec") or {}, claim.get("status") or {}
    phase = status.get("phase", "")
    bound = phase == "Bound"
    return {
        "name": metadata.get("name", ""),
        "status": _pill(phase or "Unknown", "ok" if bound else "warn",
                        "bi-check-circle-fill" if bound else "bi-hourglass-split"),
        "size": (status.get("capacity") or {}).get("storage")
        or ((spec.get("resources") or {}).get("requests") or {}).get("storage", ""),
        "storage_class": spec.get("storageClassName", ""),
        "age": _age(metadata.get("creationTimestamp")),
    }


def _event_time(event):
    return (event.get("lastTimestamp") or event.get("eventTime")
            or (event.get("metadata") or {}).get("creationTimestamp") or "")


def _event_rows(events):
    """The newest Warning events; filtered here rather than with a field selector."""
    warnings = [e for e in events if e.get("type") == "Warning"]
    warnings.sort(key=_event_time, reverse=True)
    rows = []
    for event in warnings[:MAX_EVENTS]:
        involved = event.get("involvedObject") or {}
        when = _event_time(event)
        rows.append({
            "age": _age(when[:19] + "Z" if len(when) >= 19 else when),
            "object": "{} {}".format(involved.get("kind", ""), involved.get("name", "")).strip(),
            "reason": event.get("reason", ""),
            "message": event.get("message", ""),
            "count": event.get("count") or 1,
        })
    return rows, len(warnings)


def _owned_by_ingress(route):
    """OpenShift creates a Route for each host of an Ingress, owned by it; the
    Ingress row already shows it."""
    return any(owner.get("kind") == "Ingress" for owner in (route.get("metadata") or {}).get("ownerReferences") or [])


# ---------------------------------------------------------------------------
# Alerts: what the cluster's monitoring is firing for the project
# ---------------------------------------------------------------------------
# Read from the Thanos Querier route of OpenShift's monitoring stack, which
# answers for the platform's rules and, where monitoring for user-defined
# projects is on, for the rules in the project itself. The route serves the
# Prometheus HTTP API under /api and takes the bearer token the API server
# takes; the token's identity needs the cluster-monitoring-view role.
#   Route (path /api, port web):
#     https://github.com/openshift/cluster-monitoring-operator/blob/release-4.18/assets/thanos-querier/route.yaml
#   GET /api/v1/alerts (labels, annotations, state, activeAt):
#     https://prometheus.io/docs/prometheus/latest/querying/api/#alerts
#   cluster-monitoring-view:
#     https://github.com/openshift/cluster-monitoring-operator/blob/release-4.18/assets/cluster-monitoring-operator/cluster-role-view.yaml
# The call cannot filter on an alert's labels, so the project's alerts are
# picked out here by their namespace label; an alert without one is a
# cluster-wide condition and is left out. OpenShift's default pod and workload
# rules watch only its own namespaces (openshift-*, kube-*, default):
#   https://github.com/openshift/cluster-monitoring-operator/blob/release-4.18/assets/control-plane/prometheus-rule.yaml
# so a project shows the alerts of OpenShift Virtualization, of a managed
# service's own rules, and of any rules in the project.
MONITORING_NAMESPACE = "openshift-monitoring"
THANOS_ROUTE = "thanos-querier"
# (connect, read) seconds: monitoring must never hold up the inventory for long.
MONITORING_TIMEOUT = (5, 20)
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")

# Route host of each cluster's Thanos Querier by API URL, for the life of the
# web process.
_thanos_hosts = {}

# Severity label values (lower-cased; some rules capitalize them) as
# (text, pill style, icon). Red and amber mark critical and warning alerts only.
ALERT_SEVERITIES = {
    "critical": ("Critical", "bad", "bi-exclamation-octagon-fill"),
    "warning": ("Warning", "warn", "bi-exclamation-triangle-fill"),
    "info": ("Info", "muted", "bi-info-circle"),
}
SEVERITY_ORDER = ["critical", "warning", "info"]

# Labels that name the object an alert is about, most specific first, with
# the kind shown. kube-state-metrics names the object in a label of its kind:
#   https://github.com/kubernetes/kube-state-metrics/tree/main/docs/metrics
# The service, container and instance labels are left out: on most alerts
# they describe the scrape target, not the object.
ALERT_OBJECT_LABELS = [
    ("deployment", "Deployment"),
    ("statefulset", "StatefulSet"),
    ("daemonset", "DaemonSet"),
    ("job_name", "Job"),
    ("cronjob", "CronJob"),
    ("horizontalpodautoscaler", "Horizontal pod autoscaler"),
    ("persistentvolumeclaim", "Volume claim"),
    ("poddisruptionbudget", "Disruption budget"),
    ("resourcequota", "Resource quota"),
    ("pod", "Pod"),
]
# The inventory rows that carry an alert count, by object kind: a section
# name, or for workloads the row's own kind.
ALERT_SECTIONS = {
    "Virtual machine": "vm", "Deployment": "Deployment", "StatefulSet": "StatefulSet",
    "DaemonSet": "DaemonSet", "Pod": "pod", "Volume claim": "claim",
}


def _thanos_host(lz, client):
    """Host of the cluster's thanos-querier route: read from the Route, else
    the default host OpenShift gives it, <route>-<namespace>.apps.<cluster
    domain>, taken from the API server's api.<cluster domain>; '' when neither
    is known.
    https://docs.redhat.com/en/documentation/openshift_container_platform/4.16/html/network_apis/route-route-openshift-io-v1"""
    if client.api_url in _thanos_hosts:
        return _thanos_hosts[client.api_url]
    host = ""
    try:
        route = client.get("/apis/route.openshift.io/v1/namespaces/{}/routes/{}".format(
            MONITORING_NAMESPACE, THANOS_ROUTE))
        host = ((route or {}).get("spec") or {}).get("host") or ""
    except lz.LandingZoneError as exc:
        # Reading a Route in openshift-monitoring needs more than a handler
        # scoped to its projects may have.
        logger.info("The %s route was not read: %s", THANOS_ROUTE, exc)
    if not host:
        api_host = urlparse(client.api_url).hostname or ""
        if api_host.startswith("api."):
            host = "{}-{}.apps.{}".format(THANOS_ROUTE, MONITORING_NAMESPACE, api_host[len("api."):])
    if not HOST_RE.match(host):
        return ""
    _thanos_hosts[client.api_url] = host
    return host


def _project_alerts(lz, client, namespace):
    """(alerts, problem): the firing and pending alerts labelled with the
    project's namespace as the monitoring API returns them, or why they could
    not be read."""
    if not hasattr(client, "get_from_route"):
        return [], "Alerts need the current openshift_landing_zone shared module (SHM-qmiweowv)."
    host = _thanos_host(lz, client)
    if not host:
        return [], "The cluster's monitoring route could not be found."
    try:
        body = client.get_from_route(host, "/api/v1/alerts", timeout=MONITORING_TIMEOUT)
    except lz.LandingZoneError as exc:
        status = getattr(exc, "status_code", None)
        logger.warning("Reading alerts for %s from %s failed: %s", namespace, host, exc)
        if status in (401, 403):
            return [], ("The resource handler's credentials may not read the cluster's monitoring: "
                        "they need the cluster-monitoring-view role.")
        if status is None:
            _thanos_hosts.pop(client.api_url, None)
        return [], "The cluster's monitoring could not be read: {}".format(str(exc)[:300])
    alerts = ((body or {}).get("data") or {}).get("alerts") or []
    return [
        alert for alert in alerts
        if (alert.get("labels") or {}).get("namespace") == namespace and alert.get("state") in ("firing", "pending")
    ], ""


def _owner(item, kind):
    """Name of the owner of ``kind`` in a Kubernetes object's ownerReferences, or ''."""
    for owner in (item.get("metadata") or {}).get("ownerReferences") or []:
        if owner.get("kind") == kind:
            return owner.get("name", "")
    return ""


def _alert_owners(vms, pods, budgets):
    """What ties an alert's object to a virtual machine: the VM names, and
    the VM behind each volume claim, pod and disruption budget.
    A VirtualMachine names its disks' claims in spec.template.spec.volumes
    (dataVolume.name, persistentVolumeClaim.claimName):
      https://github.com/kubevirt/api/blob/main/core/v1/schema.go
    Its virt-launcher pod and the PodDisruptionBudget KubeVirt keeps for it
    are owned by its VirtualMachineInstance, which carries the VM's name.
      https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/pod-disruption-budget-v1/"""
    claims = {}
    for vm in vms:
        name = (vm.get("metadata") or {}).get("name", "")
        template = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
        for volume in template.get("volumes") or []:
            claim = ((volume.get("dataVolume") or {}).get("name")
                     or (volume.get("persistentVolumeClaim") or {}).get("claimName"))
            if claim:
                claims[claim] = name
    return {
        "machines": {(vm.get("metadata") or {}).get("name", "") for vm in vms},
        "claims": claims,
        "pods": {(p.get("metadata") or {}).get("name", ""): _owner(p, "VirtualMachineInstance") for p in pods},
        "budgets": {(b.get("metadata") or {}).get("name", ""): _owner(b, "VirtualMachineInstance") for b in budgets},
    }


def _alert_object(labels, owners):
    """(kind, name, virtual machine) of the object an alert is about, each ''
    when unknown. OpenShift Virtualization's alerts name the VM in vmi, vm or
    name, and carry the pod of the KubeVirt component that reported it, so a
    VM is looked for first and a pod counts only when it is in the project.
      https://github.com/kubevirt/kubevirt/blob/release-1.5/pkg/monitoring/rules/alerts/vms.go"""
    machine = labels.get("vmi") or labels.get("vm") or ""
    if not machine and labels.get("name") in owners["machines"]:
        machine = labels["name"]
    if machine:
        return "Virtual machine", machine, machine
    for label, kind in ALERT_OBJECT_LABELS:
        name = labels.get(label)
        if not name:
            continue
        if label == "pod":
            if name not in owners["pods"]:
                continue
            return kind, name, owners["pods"][name]
        if label == "persistentvolumeclaim":
            return kind, name, owners["claims"].get(name, "")
        if label == "poddisruptionbudget":
            return kind, name, owners["budgets"].get(name, "")
        return kind, name, ""
    return "", "", ""


def _alert_rows(alerts, owners, servers):
    """The project's alerts for display: firing before pending, critical
    first, longest-running first."""
    rows = []
    for alert in alerts:
        labels, annotations = alert.get("labels") or {}, alert.get("annotations") or {}
        severity = str(labels.get("severity") or "").lower()
        text, style, icon = ALERT_SEVERITIES.get(severity, (severity.capitalize() or "None", "muted", "bi-bell"))
        firing = alert.get("state") == "firing"
        kind, name, machine = _alert_object(labels, owners)
        active = str(alert.get("activeAt") or "")
        runbook = str(annotations.get("runbook_url") or "")
        rows.append({
            "name": labels.get("alertname", ""),
            "firing": firing,
            "severity": severity,
            "severity_text": text,
            "pill": _pill(text, style, icon) if firing else _pill("Pending", "muted", "bi-clock"),
            # Rules word their text as description, message (older and managed-service rules) or summary.
            "text": annotations.get("description") or annotations.get("message") or annotations.get("summary") or "",
            "runbook_url": runbook if runbook.startswith("https://") else "",
            "kind": kind,
            "object": name,
            "machine": machine,
            "server_url": servers.get(machine, "") if machine else "",
            "since": _age(active[:19] + "Z") if len(active) >= 19 else "",
            "since_title": "{} UTC".format(active[:16].replace("T", " ")) if len(active) >= 16 else "",
            "active": active,
        })
    rank = {severity: index for index, severity in enumerate(SEVERITY_ORDER)}
    rows.sort(key=lambda r: (not r["firing"], rank.get(r["severity"], len(rank)), r["active"], r["name"], r["object"]))
    return rows


def _mark_alerts(alerts, sections):
    """Put an ``alerts`` pill on each inventory row a firing alert is about:
    the count, in the color of the worst severity. ``sections`` maps "vm",
    "pod", "claim" and "workload" to their rows; a workload row is matched on
    its own kind. A VM also counts the alerts on its disks' claims, its pod
    and its disruption budget."""
    found = {}
    for alert in alerts:
        if not alert["firing"]:
            continue
        keys = set()
        if alert["kind"] in ALERT_SECTIONS:
            keys.add((ALERT_SECTIONS[alert["kind"]], alert["object"]))
        if alert["machine"]:
            keys.add(("vm", alert["machine"]))
        for key in keys:
            found.setdefault(key, []).append(alert["severity"])
    for section, rows in sections.items():
        for row in rows:
            severities = found.get((row.get("kind") if section == "workload" else section, row["name"]))
            if not severities:
                continue
            worst = next((s for s in SEVERITY_ORDER if s in severities), "")
            _, style, icon = ALERT_SEVERITIES.get(worst, ("", "muted", "bi-bell"))
            row["alerts"] = _pill(
                "{} alert{}".format(len(severities), "" if len(severities) == 1 else "s"), style, icon)


def _workloads_context(resource, profile):
    lz = _lz()
    namespace, client = _client(resource)
    warnings = []

    def items(label, path):
        try:
            return client.list_items(path)
        except Exception as exc:
            logger.warning("Reading %s in %s failed: %s", label, namespace, exc)
            warnings.append("{} could not be read: {}".format(label, exc))
            return []

    try:
        if client.get_namespace(namespace) is None:
            raise PanelError("Project {} no longer exists on the cluster.".format(namespace))
        path = "/namespaces/{}".format(namespace)
        quota, other_quotas = _quota_rows(
            _quota_usage(items("Resource quotas", "/api/v1" + path + "/resourcequotas")), lz.QUOTA_NAME)

        # A cluster without OpenShift Virtualization answers 404, which reads as no VMs.
        vms = items("Virtual machines", "/apis/kubevirt.io/v1" + path + "/virtualmachines")
        instances = {}
        if vms:
            instances = {
                (i.get("metadata") or {}).get("name", ""): i
                for i in items("Virtual machine instances", "/apis/kubevirt.io/v1" + path + "/virtualmachineinstances")
            }
        names = [(vm.get("metadata") or {}).get("name", "") for vm in vms]
        servers = _server_urls(resource, names)
        machines = sorted(
            (_vm_row(vm, instances.get(name), servers.get(name, "")) for vm, name in zip(vms, names)),
            key=lambda r: r["name"],
        )

        workloads = (
            [_workload_row("Deployment", d) for d in items("Deployments", "/apis/apps/v1" + path + "/deployments")]
            + [_workload_row("StatefulSet", s) for s in items("StatefulSets", "/apis/apps/v1" + path + "/statefulsets")]
            + [_workload_row("DaemonSet", s) for s in items("DaemonSets", "/apis/apps/v1" + path + "/daemonsets")]
        )
        workloads.sort(key=lambda r: (r["kind"], r["name"]))
        pod_items = items("Pods", "/api/v1" + path + "/pods")
        pods = sorted((_pod_row(p) for p in pod_items), key=lambda r: r["name"])
        endpoints = (
            [_service_row(s) for s in items("Services", "/api/v1" + path + "/services")]
            + [_route_row(r) for r in items("Routes", "/apis/route.openshift.io/v1" + path + "/routes")
               if not _owned_by_ingress(r)]
            + [_ingress_row(i) for i in items("Ingresses", "/apis/networking.k8s.io/v1" + path + "/ingresses")]
        )
        claims = sorted(
            (_claim_row(c) for c in items("Volume claims", "/api/v1" + path + "/persistentvolumeclaims")),
            key=lambda r: r["name"],
        )
        events, warning_count = _event_rows(items("Events", "/api/v1" + path + "/events"))

        raw_alerts, alerts_problem = _project_alerts(lz, client, namespace)
        budgets = []
        if any((a.get("labels") or {}).get("poddisruptionbudget") for a in raw_alerts):
            budgets = items("Pod disruption budgets", "/apis/policy/v1" + path + "/poddisruptionbudgets")
    finally:
        client.close()

    alerts = _alert_rows(raw_alerts, _alert_owners(vms, pod_items, budgets), servers)
    _mark_alerts(alerts, {"vm": machines, "workload": workloads, "pod": pods, "claim": claims})
    firing = [a for a in alerts if a["firing"]]

    return {
        "namespace": namespace,
        "alerts": alerts,
        "alerts_problem": alerts_problem,
        "alerts_firing": len(firing),
        "alerts_pending": len(alerts) - len(firing),
        "alerts_critical": sum(1 for a in firing if a["severity"] == "critical"),
        "alerts_warning": sum(1 for a in firing if a["severity"] == "warning"),
        "quota": quota,
        "other_quotas": other_quotas,
        "machines": machines,
        "machines_running": sum(1 for m in machines if m["running"]),
        "workloads": workloads,
        "workloads_ready": sum(1 for w in workloads if w["status"]["style"] in ("ok", "muted")),
        "pods": pods,
        "pods_running": sum(1 for p in pods if p["running"]),
        "pods_failing": sum(1 for p in pods if p["status"]["style"] == "bad"),
        "endpoints": endpoints,
        "claims": claims,
        "events": events,
        "warning_count": warning_count,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Panels (HTML fragments loaded asynchronously by the tabs)
# ---------------------------------------------------------------------------
def _panel(request, resource_id, template, build):
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    if not _can_view(profile, resource):
        context = {"error": "You do not have permission to view this resource."}
    else:
        try:
            context = build(resource, profile)
        except PanelError as exc:
            context = {"error": str(exc)}
        except Exception as exc:
            if _is_lz_error(exc):
                logger.warning("OpenShift call failed for resource %s: %s", resource.id, exc)
                context = {"error": "OpenShift returned an error: {}".format(exc)}
            else:
                logger.exception("Panel %s failed for resource %s", template, resource.id)
                context = {"error": "Could not load this panel: {}".format(exc)}
    context["resource"] = resource
    return render(request, TEMPLATES + template, context)


def connect_panel(request, resource_id):
    return _panel(request, resource_id, "connect_panel.html", _connect_context)


def workloads_panel(request, resource_id):
    return _panel(request, resource_id, "workloads_panel.html", _workloads_context)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
# CloudBolt 2026.1.73 and later let a tab extension take a built-in tab's
# place: tab_extension(replaces="<slug>") puts the tab in that tab's slot
# (CMP-3592).
# On a project, Workloads takes Overview's slot.
# Older appliances have no such argument, and there the tabs follow the
# built-in ones as before.
_REPLACES_SUPPORTED = "replaces" in inspect.signature(tab_extension).parameters
_tab_state = threading.local()


class _BuiltInTab(str):
    """Slug of the built-in tab an extension tab replaces, true only while
    that tab is shown. CloudBolt lets the last extension that names a slug
    decide that slot whether or not its own tab is shown, and the cluster and
    namespace tabs of the EKS, AKS, GKE and OpenShift extensions all name
    Overview. A hidden tab whose slug is false names nothing and leaves the
    slot to the tab that is shown. Each tab delegate records in _tab_state
    whether its kind of resource ("cluster" or "namespace") is on the page,
    just before CloudBolt reads the slug, per thread because every request
    shares the extension object."""

    def __new__(cls, slug, kind):
        tab = super().__new__(cls, slug)
        tab.kind = kind
        return tab

    def __bool__(self):
        return bool(getattr(_tab_state, self.kind, False))


def _replaces(slug, kind):
    """tab_extension arguments that put a tab in a built-in tab's slot on
    resources of its kind."""
    return {"replaces": _BuiltInTab(slug, kind)} if _REPLACES_SUPPORTED else {}


class ProjectTabDelegate(TabExtensionDelegate):
    """OpenShift Project resources of the OpenShift Project Landing Zone blueprint."""

    def should_display(self):
        resource_type = getattr(self.instance, "resource_type", None)
        shown = getattr(resource_type, "name", "") == RESOURCE_TYPE and bool(_attr(self.instance, MARKER_FIELD))
        _tab_state.namespace = shown
        return shown


def _tab_context(resource):
    """Identity shown above the panels, from the resource's custom fields (no cluster call)."""
    return {
        "resource": resource,
        "namespace": _attr(resource, MARKER_FIELD) or resource.name,
        "cluster_name": _attr(resource, CF_PREFIX + "cluster") or "",
    }


@tab_extension(
    model=Resource,
    title="Connect",
    delegate=ProjectTabDelegate,
    description="oc commands, the cluster and console, and the OpenShift and CloudBolt access to an OpenShift project",
)
def openshift_project_connect_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    context = _tab_context(resource)
    context.update({
        "connect_url": reverse("openshift_project_connect_panel", args=[resource.id]),
        "manage_access_url": _action_url(request.get_user_profile(), resource, ACTION_MANAGE_ACCESS),
    })
    return render(request, TEMPLATES + "connect_tab.html", context)


@tab_extension(
    model=Resource,
    title="Workloads",
    delegate=ProjectTabDelegate,
    description="Read-only inventory of one OpenShift project: firing alerts, quota usage, virtual machines, "
                "workloads, pods, services and routes, storage, and warning events",
    **_replaces("overview", "namespace")
)
def openshift_project_workloads_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    context = _tab_context(resource)
    context.update({
        "profile": request.get_user_profile(),
        "workloads_url": reverse("openshift_project_workloads_panel", args=[resource.id]),
        "quota_change_url": _action_url(request.get_user_profile(), resource, ACTION_REQUEST_QUOTA_CHANGE),
    })
    return render(request, TEMPLATES + "workloads_tab.html", context)
