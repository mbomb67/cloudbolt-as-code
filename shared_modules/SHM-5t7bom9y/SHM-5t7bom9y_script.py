"""Services, Routes and NetworkPolicies for OpenShift Virtualization VMs, through the cluster API.

A VM's pod IP changes whenever the VM is stopped and started, so anything that talks to a VM should
use a Service name, and anything that restricts traffic to a VM should select its pod by label.
Every object this module creates selects the VM's virt-launcher pod by the label KubeVirt puts on
it (vm.kubevirt.io/name) and carries the owning CloudBolt resource in a label, so one call removes
everything a resource created.

    from shared_modules import openshift_vm_network as vmnet

    cluster, namespace = vmnet.for_server(server)
    labels = vmnet.owner_labels(resource)
    fqdn = vmnet.expose_port(cluster, namespace, server.hostname, 3306, labels)   # ClusterIP Service
    host = vmnet.publish_route(cluster, namespace, server.hostname, 443, labels)  # public HTTPS Route
    vmnet.restrict_ingress(cluster, namespace, server.hostname,
                           [vmnet.from_vm("app-001", 3306)], labels)              # NetworkPolicy
    vmnet.delete_owned(cluster, namespace, resource)                              # teardown

Calls raise OpenShiftApiError with the API's own message. Only the OpenShift Virtualization handler
(resourcehandlers/ovirt) is supported, because the API URL and token come from its API wrapper.
"""

import requests

VM_LABEL = "vm.kubevirt.io/name"
OWNER_LABEL = "cloudbolt.io/resource"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY = "cloudbolt"

SERVICES = "/api/v1/namespaces/{namespace}/services"
ROUTES = "/apis/route.openshift.io/v1/namespaces/{namespace}/routes"
NETWORK_POLICIES = "/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies"

# KubeVirt's port-forward, which carries CloudBolt's SSH to the guest, is dialled by the virt-api
# pods in the namespace the virtualization operator is installed in.
# https://github.com/kubevirt/kubevirt/blob/release-1.6/pkg/virt-api/rest/dialers.go
KUBEVIRT_NAMESPACE = "openshift-cnv"
SSH_PORT = 22

# (connect, read) seconds, so an unreachable cluster fails the job instead of hanging it.
REQUEST_TIMEOUT = (10, 60)


class OpenShiftApiError(Exception):
    """The cluster API refused a call or could not be reached."""


class Cluster:
    """A small REST client that reuses an OpenShift Virtualization handler's API URL and token."""

    def __init__(self, handler):
        client = handler.get_api_wrapper().client
        authorization = client.http_session.headers.get("Authorization")
        if not authorization:
            client._login()
            authorization = client.http_session.headers.get("Authorization")
        if not authorization:
            raise OpenShiftApiError(
                f"Resource handler '{handler.name}' did not authenticate to OpenShift; "
                "check its credentials on the resource handler page."
            )
        self.api_url = client.openshift_base_api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": authorization, "Accept": "application/json"})
        # The handler's own setting combined with Admin > SSL Certificates: False or a CA bundle path.
        self.session.verify = handler.get_ssl_verification()

    def request(self, method, path, body=None, params=None, content_type="application/json", allow_404=False):
        """Send one call; return the parsed JSON, or None for a 404 when allow_404 is set."""
        try:
            response = self.session.request(
                method,
                f"{self.api_url}{path}",
                json=body,
                params=params,
                headers={"Content-Type": content_type} if body is not None else None,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as error:
            raise OpenShiftApiError(f"{method} {path} could not reach {self.api_url}: {error}")
        if response.status_code == 404 and allow_404:
            return None
        if not response.ok:
            try:
                detail = response.json().get("message", response.text[:500])
            except ValueError:
                detail = response.text[:500]
            raise OpenShiftApiError(f"{method} {path} failed ({response.status_code}): {detail}")
        return response.json() if response.text else {}

    def apply(self, collection, body):
        """Create the object, or merge the body into the one that already has its name.

        A merge patch replaces lists whole, so ports and rules end up exactly as given, and it keeps
        the fields the API assigned (a Service's clusterIP, a Route's host), which a PUT would have
        to copy back.
        https://kubernetes.io/docs/reference/using-api/api-concepts/#patch-and-apply
        """
        name = body["metadata"]["name"]
        if self.request("GET", f"{collection}/{name}", allow_404=True) is None:
            return self.request("POST", collection, body=body)
        return self.request(
            "PATCH", f"{collection}/{name}", body=body, content_type="application/merge-patch+json"
        )


def for_server(server):
    """Return (Cluster, namespace) for an OpenShift Virtualization VM."""
    handler = server.resource_handler.cast()
    if not hasattr(server, "ovirtserverinfo"):
        raise OpenShiftApiError(f"{server.hostname} is not an OpenShift Virtualization VM.")
    return Cluster(handler), server.ovirtserverinfo.namespace


def for_handler_id(handler_id):
    """Return a Cluster for an OpenShift Virtualization resource handler's id, for use after its VMs are gone."""
    from resourcehandlers.models import ResourceHandler

    return Cluster(ResourceHandler.objects.get(id=int(handler_id)).cast())


def owner_labels(resource):
    """The labels that mark an object as created by CloudBolt for this resource."""
    return {MANAGED_BY_LABEL: MANAGED_BY, OWNER_LABEL: _owner_value(resource)}


def _owner_value(resource):
    return getattr(resource, "global_id", None) or f"resource-{resource.id}"


def service_fqdn(name, namespace):
    return f"{name}.{namespace}.svc.cluster.local"


def expose_port(cluster, namespace, vm_name, port, labels, service_name=None):
    """Create or update a ClusterIP Service for one port of a VM; return the Service's DNS name.

    The Service is named after the VM unless service_name is given. VM names are unique in a
    namespace, so two deployments never collide.
    https://kubernetes.io/docs/reference/kubernetes-api/service-resources/service-v1/
    """
    name = service_name or vm_name
    port = int(port)
    collection = SERVICES.format(namespace=namespace)
    existing = cluster.request("GET", f"{collection}/{name}", allow_404=True) or {}
    ports = [p for p in existing.get("spec", {}).get("ports", []) if p.get("port") != port]
    ports.append({"name": f"tcp-{port}", "protocol": "TCP", "port": port, "targetPort": port})
    cluster.apply(collection, {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(labels)},
        "spec": {"type": "ClusterIP", "selector": {VM_LABEL: vm_name}, "ports": ports},
    })
    return service_fqdn(name, namespace)


def publish_route(cluster, namespace, service_name, port, labels, termination="reencrypt", route_name=None):
    """Create or update a Route to a Service port; return the hostname the router assigned.

    termination is edge, passthrough or reencrypt. With reencrypt the router shows browsers its own
    certificate and opens a second TLS connection to the VM, which it trusts only once
    set_route_destination_ca has given it the VM's certificate.
    https://docs.redhat.com/en/documentation/openshift_container_platform/4.19/html/network_apis/route-route-openshift-io-v1
    """
    name = route_name or service_name
    collection = ROUTES.format(namespace=namespace)
    route = cluster.apply(collection, {
        "apiVersion": "route.openshift.io/v1",
        "kind": "Route",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(labels)},
        "spec": {
            "to": {"kind": "Service", "name": service_name, "weight": 100},
            "port": {"targetPort": int(port)},
            "tls": {"termination": termination, "insecureEdgeTerminationPolicy": "Redirect"},
            "wildcardPolicy": "None",
        },
    })
    host = route.get("spec", {}).get("host")
    if not host:
        host = (cluster.request("GET", f"{collection}/{name}").get("spec") or {}).get("host")
    if not host:
        raise OpenShiftApiError(f"Route {name} was created but the router gave it no hostname.")
    return host


def get_route(cluster, namespace, name):
    """The Route object, or None when it does not exist."""
    return cluster.request("GET", f"{ROUTES.format(namespace=namespace)}/{name}", allow_404=True)


def set_route_destination_ca(cluster, namespace, name, ca_pem):
    """Tell a reencrypt Route which certificate to trust on the VM (spec.tls.destinationCACertificate)."""
    cluster.request(
        "PATCH",
        f"{ROUTES.format(namespace=namespace)}/{name}",
        body={"spec": {"tls": {"destinationCACertificate": ca_pem}}},
        content_type="application/merge-patch+json",
    )


def from_vm(vm_name, port):
    """An ingress rule: this port, from one VM in the same namespace."""
    return {
        "from": [{"podSelector": {"matchLabels": {VM_LABEL: vm_name}}}],
        "ports": [{"protocol": "TCP", "port": int(port)}],
    }


def from_router(port):
    """An ingress rule: this port, from the cluster's ingress routers.

    https://docs.redhat.com/en/documentation/openshift_container_platform/4.19/html/network_security/network-policy#nw-networkpolicy-allow-from-router_about-network-policy
    """
    return {
        "from": [{"namespaceSelector": {"matchLabels": {"policy-group.network.openshift.io/ingress": ""}}}],
        "ports": [{"protocol": "TCP", "port": int(port)}],
    }


def from_namespace(namespace, port):
    """An ingress rule: this port, from every pod in another namespace."""
    return {
        "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}}}],
        "ports": [{"protocol": "TCP", "port": int(port)}],
    }


def restrict_ingress(cluster, namespace, vm_name, rules, labels, keep_api_tunnel=True):
    """Create or replace the NetworkPolicy that limits what can connect to a VM; return its name.

    Once a policy selects a pod, every inbound connection no rule allows is dropped, so the rules
    given here are the complete list. keep_api_tunnel adds SSH from the KubeVirt API pods, without
    which CloudBolt can no longer run scripts on the VM.
    https://kubernetes.io/docs/concepts/services-networking/network-policies/
    """
    name = f"{vm_name}-ingress"
    rules = list(rules)
    if keep_api_tunnel:
        rules.append(from_namespace(KUBEVIRT_NAMESPACE, SSH_PORT))
    cluster.apply(NETWORK_POLICIES.format(namespace=namespace), {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(labels)},
        "spec": {
            "podSelector": {"matchLabels": {VM_LABEL: vm_name}},
            "policyTypes": ["Ingress"],
            "ingress": rules,
        },
    })
    return name


def delete_owned(cluster, namespace, resource):
    """Delete every Route, Service and NetworkPolicy labelled for this resource; return what went.

    Routes go first so nothing public points at a Service that is about to disappear, and policies
    go last so the VMs stay restricted until the end.
    """
    selector = f"{OWNER_LABEL}={_owner_value(resource)}"
    deleted = []
    for kind, template in (("Route", ROUTES), ("Service", SERVICES), ("NetworkPolicy", NETWORK_POLICIES)):
        collection = template.format(namespace=namespace)
        listing = cluster.request("GET", collection, params={"labelSelector": selector}, allow_404=True) or {}
        for item in listing.get("items", []):
            name = item["metadata"]["name"]
            cluster.request("DELETE", f"{collection}/{name}", allow_404=True)
            deleted.append(f"{kind} {name}")
    return deleted
