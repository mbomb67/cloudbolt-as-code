"""
CloudBolt day-2 resource action: Deploy Test Workload.

One action shared by the four namespace landing-zone blueprints: AWS EKS
Namespace (BP-e4c0q58z), AKS Namespace (BP-dogqi827), GKE Namespace
(BP-c9ca1i3z) and OpenShift Project Landing Zone (BP-tikkhf2y). It deploys a
small web app into the namespace so its Workloads tab has something to show:
a Deployment of an unprivileged nginx and a ClusterIP Service in front of it,
both named cb-test-workload. Remove deletes both again; tearing down the
namespace removes them too.

Which cluster to talk to comes from the fields each blueprint's build stores
on the resource, and the API client is the one that blueprint's own shared
module uses, so the action reaches the cluster with the same credentials as
the rest of the landing zone:

  eks_namespace_name     shared_modules/SHM-sa5yqm9j (eks_namespace)
  aks_namespace_name     shared_modules/SHM-cozv547n (aks_namespace)
  gke_namespace_name     shared_modules/SHM-kephq8iq (gke_namespace)
  openshift_project_name shared_modules/SHM-qmiweowv (openshift_landing_zone)

The pod passes the Pod Security "restricted" profile and OpenShift's
restricted-v2 SCC: non-root (the image's numeric USER 101, or the UID
OpenShift assigns, which the image supports by giving group 0 write access to
its cache and config), no privilege escalation, all capabilities dropped,
RuntimeDefault seccomp. Explicit requests and limits keep it inside every
size tier's ResourceQuota.

Vendor references:
  Image   https://github.com/nginx/docker-nginx-unprivileged
          (Dockerfile: USER $UID with UID=101, EXPOSE 8080, chown $UID:0 + chmod g+w)
  Deployment  https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/
  Deployment status (ReplicaFailure / FailedCreate)
              https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#deployment-status
  Service     https://kubernetes.io/docs/reference/kubernetes-api/service-resources/service-v1/
  Pod         https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/
  Pod Security Standards (restricted)
              https://kubernetes.io/docs/concepts/security/pod-security-standards/
  Deletion cascades in the background by default
              https://kubernetes.io/docs/concepts/architecture/garbage-collection/

Action Inputs:
  - k8s_test_workload_action (STR, required): deploy or remove.
  - k8s_test_workload_replicas (INT): pod count for deploy, 1 to 5.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

import time

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

NAME = "cb-test-workload"
IMAGE = "docker.io/nginx/nginx-unprivileged:stable-alpine-slim"
CONTAINER_PORT = 8080
SELECTOR = {"app.kubernetes.io/name": NAME}
LABELS = dict(SELECTOR, **{"app.kubernetes.io/managed-by": "cloudbolt"})
MAX_REPLICAS = 5
ROLLOUT_TIMEOUT = 180
POLL_SECONDS = 5


def generate_options_for_k8s_test_workload_action(field, **kwargs):
    return {"options": [("deploy", "Deploy"), ("remove", "Remove")], "initial_value": "deploy", "sort": False}


def generate_options_for_k8s_test_workload_replicas(field, **kwargs):
    return {"options": [(n, str(n)) for n in range(1, MAX_REPLICAS + 1)], "initial_value": 2, "sort": False}


class Cluster:
    """The namespace and the Kubernetes calls this action needs, over the
    client of whichever landing-zone module built the resource. The modules'
    create-or-replace helper is reused as is so this action needs no change
    to any of them."""

    def __init__(self, kind, namespace, cluster_name, apply, get, delete, error):
        self.kind = kind
        self.namespace = namespace
        self.cluster_name = cluster_name
        self.apply = apply
        self.get = get
        self.delete = delete
        self.error = error


def _orchestrator_cluster(kind, module):
    """EKS, AKS and GKE namespaces: the Kubernetes container orchestrator the
    namespace was created through (<prefix>name, <prefix>orchestrator_id)."""

    def load(resource):
        namespace, orchestrator, cluster_name = module.load_cluster(resource)
        client = module.NamespaceClient.from_orchestrator(orchestrator)
        if kind == "GKE":
            get = lambda path, params=None: client.get(path, params=params)  # noqa: E731
            delete = lambda path: client._call("delete", path)  # noqa: E731
        else:
            get = lambda path, params=None: client._request("GET", path, params=params, allow_404=True)  # noqa: E731
            delete = lambda path: client._request("DELETE", path, allow_404=True) is not None  # noqa: E731
        return Cluster(kind, namespace, cluster_name, client._apply, get, delete, module.NamespaceError)

    return load


def _openshift_cluster(resource):
    """OpenShift projects: the OpenShift Virtualization handler the project
    was created on (openshift_project_name, openshift_project_rh_id)."""
    from shared_modules import openshift_landing_zone as lz

    namespace, handler = lz.load_handler(resource)
    client = lz.OpenShiftLandingZoneClient.from_handler(handler)
    return Cluster(
        "OpenShift",
        namespace,
        handler.name,
        client._apply,
        lambda path, params=None: client._request("GET", path, params=params, allow_404=True),
        lambda path: client._request("DELETE", path, allow_404=True) is not None,
        lz.LandingZoneError,
    )


def _connect(resource):
    """The Cluster for a namespace resource, picked by the name field its
    blueprint stores. Each module is imported only when needed, so a problem
    in one cloud's module never blocks the others."""
    values = resource.get_cf_values_as_dict()
    if values.get("eks_namespace_name"):
        from shared_modules import eks_namespace

        return _orchestrator_cluster("EKS", eks_namespace)(resource)
    if values.get("aks_namespace_name"):
        from shared_modules import aks_namespace

        return _orchestrator_cluster("AKS", aks_namespace)(resource)
    if values.get("gke_namespace_name"):
        from shared_modules import gke_namespace

        return _orchestrator_cluster("GKE", gke_namespace)(resource)
    if values.get("openshift_project_name"):
        return _openshift_cluster(resource)
    raise ValueError(
        "this resource is not an EKS, AKS or GKE namespace or an OpenShift project "
        "(no eks_namespace_name, aks_namespace_name, gke_namespace_name or openshift_project_name)"
    )


def _deployment(namespace, replicas):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": NAME, "namespace": namespace, "labels": LABELS},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": SELECTOR},
            "template": {
                "metadata": {"labels": LABELS},
                "spec": {
                    "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [
                        {
                            "name": "web",
                            "image": IMAGE,
                            "ports": [{"name": "http", "containerPort": CONTAINER_PORT}],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "32Mi"},
                                "limits": {"cpu": "100m", "memory": "64Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "readinessProbe": {"httpGet": {"path": "/", "port": "http"}, "periodSeconds": 5},
                        }
                    ],
                },
            },
        },
    }


def _service(namespace):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": NAME, "namespace": namespace, "labels": LABELS},
        "spec": {
            "type": "ClusterIP",
            "selector": SELECTOR,
            "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
        },
    }


def _pod_problems(cluster, pods_path):
    """Why pods are not ready: each container's waiting reason and message
    (ImagePullBackOff, CrashLoopBackOff, ...) or the pod's unschedulable
    condition."""
    problems = []
    selector = ",".join("{}={}".format(k, v) for k, v in SELECTOR.items())
    pods = (cluster.get(pods_path, params={"labelSelector": selector}) or {}).get("items") or []
    for pod in pods:
        name = pod["metadata"]["name"]
        status = pod.get("status") or {}
        for container in status.get("containerStatuses") or []:
            waiting = (container.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") and waiting["reason"] != "ContainerCreating":
                problems.append("{}: {} {}".format(name, waiting["reason"], waiting.get("message", "")).strip())
        for condition in status.get("conditions") or []:
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                problems.append("{}: {} {}".format(name, condition.get("reason", ""), condition.get("message", "")).strip())
    return problems


def _wait_for_rollout(cluster, replicas):
    """Wait until every replica is available. Returns (ready, detail)."""
    ns = cluster.namespace
    deployment_path = "/apis/apps/v1/namespaces/{}/deployments/{}".format(ns, NAME)
    pods_path = "/api/v1/namespaces/{}/pods".format(ns)
    deadline = time.time() + ROLLOUT_TIMEOUT
    available = 0
    while True:
        deployment = cluster.get(deployment_path) or {}
        status = deployment.get("status") or {}
        available = status.get("availableReplicas") or 0
        if status.get("observedGeneration", 0) >= deployment.get("metadata", {}).get("generation", 1) \
                and status.get("updatedReplicas") == replicas and available == replicas:
            return True, "{}/{} pods available".format(available, replicas)
        for condition in status.get("conditions") or []:
            # Quota and admission refusals surface here, not on any pod.
            if condition.get("type") == "ReplicaFailure" and condition.get("status") == "True":
                return False, "pods could not be created: {}".format(condition.get("message", condition.get("reason")))
        if time.time() >= deadline:
            problems = _pod_problems(cluster, pods_path)
            detail = "; ".join(problems) if problems else "pods are still starting"
            return False, "{}/{} pods available after {}s ({})".format(available, replicas, ROLLOUT_TIMEOUT, detail)
        set_progress("Waiting for pods: {}/{} available".format(available, replicas))
        time.sleep(POLL_SECONDS)


def _deploy(cluster, replicas):
    ns = cluster.namespace
    set_progress("Applying Deployment {} ({} replica(s), {}) in {}".format(NAME, replicas, IMAGE, ns))
    cluster.apply("/apis/apps/v1/namespaces/{}/deployments".format(ns), NAME, _deployment(ns, replicas))
    # The Service never changes, so an existing one is left alone: replacing
    # it would mean carrying over the clusterIP the API server assigned.
    services = "/api/v1/namespaces/{}/services".format(ns)
    if cluster.get("{}/{}".format(services, NAME)) is None:
        set_progress("Creating Service {} in {}".format(NAME, ns))
        cluster.apply(services, NAME, _service(ns))
    ready, detail = _wait_for_rollout(cluster, replicas)
    where = "{} namespace {} on {}".format(cluster.kind, ns, cluster.cluster_name)
    if ready:
        return "SUCCESS", "Test workload {} is running in {}: {}.".format(NAME, where, detail), ""
    return "WARNING", "Test workload {} was applied in {}.".format(NAME, where), detail


def _remove(cluster):
    ns = cluster.namespace
    removed = []
    for kind, path in (
        ("Service", "/api/v1/namespaces/{}/services/{}".format(ns, NAME)),
        ("Deployment", "/apis/apps/v1/namespaces/{}/deployments/{}".format(ns, NAME)),
    ):
        set_progress("Deleting {} {} in {}".format(kind, NAME, ns))
        if cluster.delete(path):
            removed.append(kind)
    where = "{} namespace {} on {}".format(cluster.kind, ns, cluster.cluster_name)
    if not removed:
        return "SUCCESS", "No test workload was found in {}.".format(where), ""
    return "SUCCESS", "Deleted the test workload's {} from {}; its pods stop shortly.".format(
        " and ".join(removed), where), ""


def _resolve_resource(job, resource, kwargs):
    """Accept the resource from any of the slots CloudBolt may use."""
    if resource is not None:
        return resource
    if kwargs.get("resource") is not None:
        return kwargs["resource"]
    for candidate in kwargs.get("resources") or []:
        if candidate is not None:
            return candidate
    if job is not None:
        return job.resource_set.first()
    return None


def run(job, resource=None, **kwargs):
    """Deploy or remove the test workload in the resource's namespace."""
    resource = _resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No resource was supplied."

    action = "{{ k8s_test_workload_action }}".strip().lower() or "deploy"
    if action not in ("deploy", "remove"):
        return "FAILURE", "", "Action must be 'deploy' or 'remove', got '{}'.".format(action)
    replicas_str = "{{ k8s_test_workload_replicas }}".strip() or "2"
    try:
        replicas = int(replicas_str)
    except ValueError:
        return "FAILURE", "", "Replicas must be a whole number, got '{}'.".format(replicas_str)
    if not 1 <= replicas <= MAX_REPLICAS:
        return "FAILURE", "", "Replicas must be between 1 and {}.".format(MAX_REPLICAS)

    try:
        cluster = _connect(resource)
    except Exception as exc:
        logger.exception("Could not reach the cluster for resource %s", resource.id)
        return "FAILURE", "", "The namespace's cluster could not be reached: {}".format(exc)

    try:
        if action == "deploy":
            return _deploy(cluster, replicas)
        return _remove(cluster)
    except cluster.error as exc:
        return "FAILURE", "", "The cluster refused the request: {}".format(exc)
    except Exception as exc:
        logger.exception("Test workload %s failed in namespace %s", action, cluster.namespace)
        return "FAILURE", "", "The {} failed: {}".format(action, exc)
