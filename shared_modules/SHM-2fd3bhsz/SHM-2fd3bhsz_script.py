"""
StormForge agent on the Kubernetes clusters CloudBolt builds.

Shared by the AWS EKS (BP-r5apcf7e), Azure Kubernetes Service (BP-20q58q9z)
and Google Kubernetes Engine (BP-g8g8aecx) blueprints and by the Install or
Upgrade StormForge Agent action (RSA-76jmctg6):

  install_or_upgrade(resource, version)
      Registers a StormForge credential for the cluster (once), writes it to
      the stormforge-auth Secret, and runs `helm upgrade --install` of the
      StormForge chart in a short-lived Job inside the cluster.
  remove(resource)
      Revokes the cluster's StormForge credential and deletes the cluster in
      StormForge. Runs after the cluster itself is deleted.
  version_options(resource)
      Dropdown entries for the action: installed version -> target version.

Every StormForge call goes to the StormForge API; nothing uses the StormForge
CLI. The org-level credential lives in a CloudBolt connection info named
"StormForge": Username = client ID, Password = client secret, IP/Hostname =
the StormForge API host of the customer's StormForge endpoint (for example
api.stormforge.io). It must be allowed to manage clients (cluster
credentials) and clusters. Nothing else names an endpoint: the issuer comes
from discovery, the agent is pointed at the same API host, and its metrics go
to the in. host beside the api. host.

  Discovery   GET  https://<api>/.well-known/openid-configuration
              (issuer, token_endpoint, registration_endpoint)
  Token       POST token_endpoint, grant_type=client_credentials, audience=<api>,
              scope=manage:clients manage:clusters view:clusters
              https://www.rfc-editor.org/rfc/rfc6749#section-4.4
  Credential  POST registration_endpoint {"client_name", "grant_types"}
              (OAuth 2.0 Dynamic Client Registration)
              https://www.rfc-editor.org/rfc/rfc7591#section-3.1
              read and delete at the client's registration URI
              https://www.rfc-editor.org/rfc/rfc7592#section-2
  Cluster     GET | DELETE https://<api>/v3/clusters/<name>
              (StormForge API v3; status.phase, kubernetes.systemNamespaceUID,
              stormforge.products[].version)

The agent install follows StormForge's documented chart and externally
managed Secret:
  https://docs.stormforge.io/docs/installation/install/
  https://docs.stormforge.io/docs/guides/manage-secrets/
  chart oci://registry.stormforge.io/library/stormforge (values clusterName,
  createAuthSecret, clusterProvider, clusterLabels, proxyUrl, noProxy,
  authorization.issuer, stormforge.address, forwarder.remoteWriteUrl; Secret
  keys STORMFORGE_CLIENT_ID, STORMFORGE_CLIENT_SECRET, STORMFORGE_ISSUER,
  STORMFORGE_SERVER). The chart defaults the last three to the production
  endpoint (https://api.stormforge.io/ and
  https://in.stormforge.io/prometheus/write), so every install sets them.

Helm runs inside the cluster, so neither the appliance nor the cluster needs
a helm install. The Job uses a public Helm 4 image pinned by digest
(https://github.com/alpine-docker/helm) under a temporary ServiceAccount
bound to cluster-admin, which the chart needs because it creates CRDs and
ClusterRoles. The Job, its ConfigMap, the ServiceAccount and the binding are
deleted when Helm finishes.
  Jobs              https://kubernetes.io/docs/concepts/workloads/controllers/job/
  helm upgrade      https://helm.sh/docs/helm/helm_upgrade/
  Helm 3 support ends 2026-11-11  https://helm.sh/blog/helm-4-released/

Optional parameters on the cluster's environment (Admin > Parameters, then
the environment's Parameters tab):
  stormforge_helm_image   Helm 4 image, e.g. a copy in a private registry
  stormforge_proxy_url    HTTPS proxy the agent uses to reach StormForge
  stormforge_no_proxy     NO_PROXY list for the agent (chart default otherwise)

Cluster egress the agent needs: the API host, its in. host,
registry.stormforge.io and quay.io on 443, plus the Helm image's registry.
"""

import base64
import datetime
import gzip
import json
import re
import time
import urllib.parse

import requests

from common.methods import get_proxies, set_progress
from infrastructure.models import CustomField, Environment
from utilities.exceptions import CloudBoltException
from utilities.helpers import get_ssl_verification
from utilities.logger import ThreadLogger
from utilities.models import ConnectionInfo

logger = ThreadLogger(__name__)

CONNECTION_INFO_NAME = "StormForge"
# What the org credential does: register and revoke cluster credentials, read
# and delete clusters.
ORG_SCOPES = ("manage:clients", "manage:clusters", "view:clusters")

CHART = "oci://registry.stormforge.io/library/stormforge"
CHART_REGISTRY = "https://registry.stormforge.io"
CHART_REPOSITORY = "library/stormforge"
RELEASE = "stormforge"
NAMESPACE = "stormforge-system"
AUTH_SECRET = "stormforge-auth"

# alpine/helm 4.3.1 (linux amd64, arm64 and others), pinned by digest because
# the image's version tags are rebuilt in place.
DEFAULT_HELM_IMAGE = (
    "docker.io/alpine/helm:4.3.1"
    "@sha256:e3f8d5c462d75470fbb1fce79ad5c20063657096d1f98c0fcea869e2600e2e5e"
)
INSTALLER = "cloudbolt-stormforge-installer"
HELM_TIMEOUT = "10m"
JOB_DEADLINE_SECONDS = 900
POLL_SECONDS = 5
IMAGE_FAILURE_GRACE_SECONDS = 90
CONNECT_WAIT_SECONDS = 120
REQUEST_TIMEOUT = 30

MANAGED_LABELS = {"app.kubernetes.io/managed-by": "cloudbolt"}

CLUSTER_ORCHESTRATOR_FIELD = "container_orchestrator_id"

# Which blueprint built the cluster: the field its build stores, and the
# chart's clusterProvider value.
PROVIDERS = (
    ("eks_cluster_name", "EKS", "aws"),
    ("aks_cluster_name", "AKS", "azure"),
    ("gke_cluster_name", "GKE", "gcp"),
)

FIELD_CLUSTER = "stormforge_cluster_name"
FIELD_VERSION = "stormforge_agent_version"
FIELD_CLIENT = "stormforge_client_id"
FIELD_STATUS = "stormforge_status"

ENV_HELM_IMAGE = "stormforge_helm_image"
ENV_PROXY_URL = "stormforge_proxy_url"
ENV_NO_PROXY = "stormforge_no_proxy"

# (name, label, type, description, show_as_attribute)
RESOURCE_FIELDS = [
    (FIELD_CLUSTER, "StormForge Cluster", "STR",
     "Name of this cluster in StormForge.", True),
    (FIELD_VERSION, "StormForge Agent Version", "STR",
     "StormForge chart version CloudBolt last installed on this cluster.", True),
    (FIELD_STATUS, "StormForge Status", "STR",
     "Result of CloudBolt's last StormForge install, upgrade or removal on this cluster.", True),
    (FIELD_CLIENT, "StormForge Credential ID", "STR",
     "Client ID of the StormForge credential CloudBolt registered for this cluster.", False),
]
ENVIRONMENT_FIELDS = [
    (ENV_HELM_IMAGE, "StormForge Helm Image", "STR",
     "Helm 4 container image CloudBolt runs in the cluster to install the StormForge agent. "
     "Blank uses a public image pinned by digest."),
    (ENV_PROXY_URL, "StormForge Proxy URL", "STR",
     "HTTPS proxy the StormForge agent uses to reach StormForge (the chart's proxyUrl)."),
    (ENV_NO_PROXY, "StormForge No Proxy", "STR",
     "Comma-separated NO_PROXY list for the StormForge agent (the chart's noProxy)."),
]


class StormForgeError(Exception):
    """A StormForge API, registry or Kubernetes step failed; the message says which."""


def ensure_custom_fields():
    """Create the resource and environment fields this module uses. Idempotent."""
    for name, label, cf_type, description, show_as_attribute in RESOURCE_FIELDS:
        CustomField.objects.get_or_create(
            name=name,
            defaults=dict(label=label, description=description, type=cf_type,
                          show_on_servers=False, show_as_attribute=show_as_attribute),
        )
    for name, label, cf_type, description in ENVIRONMENT_FIELDS:
        CustomField.objects.get_or_create(
            name=name,
            defaults=dict(label=label, description=description, type=cf_type,
                          show_on_servers=False, show_as_attribute=False),
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _http(method, url, **kwargs):
    """requests with CloudBolt's SSL trust settings and appliance proxy."""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs.setdefault("verify", get_ssl_verification())
    try:
        kwargs.setdefault("proxies", get_proxies(url))
    except Exception:  # noqa: BLE001 - a proxy misconfiguration should not hide the real call
        kwargs.setdefault("proxies", {})
    try:
        return requests.request(method, url, **kwargs)
    except requests.exceptions.RequestException as exc:
        host = urllib.parse.urlparse(url).netloc
        raise StormForgeError(f"Could not reach {host}: {exc}")


def _error_text(response):
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:300]
    if isinstance(body, dict):
        for key in ("error_description", "detail", "message", "error", "title"):
            if body.get(key):
                return str(body[key])
    return json.dumps(body)[:300]


def cluster_name_for_stormforge(name):
    """A cluster name StormForge accepts: an RFC 1123 label (a-z, 0-9, '-', at most 63)."""
    value = re.sub(r"[^a-z0-9-]+", "-", str(name or "").strip().lower())
    value = re.sub(r"-{2,}", "-", value).strip("-")[:63].rstrip("-")
    if not value:
        raise StormForgeError(f"'{name}' does not give a usable StormForge cluster name.")
    return value


def _version_key(version):
    return tuple(int(part) for part in version.split("."))


def _stable(version):
    return bool(re.fullmatch(r"\d+\.\d+\.\d+", version or ""))


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


def _environment_setting(resource, name):
    """A stormforge_* parameter value from the cluster's environment, or ''."""
    env_id = resource.get_cf_values_as_dict().get("env_id")
    if env_id in (None, ""):
        return ""
    try:
        env = Environment.objects.filter(id=int(str(env_id).strip())).first()
    except (TypeError, ValueError):
        return ""
    if env is None:
        return ""
    try:
        return str(env.get_cfv(name) or "").strip()
    except Exception:  # noqa: BLE001 - an unset or missing parameter means "use the default"
        return ""


def _set_fields(resource, **values):
    """Record values on the resource; a bookkeeping failure never hides the real result."""
    for name, value in values.items():
        try:
            resource.set_value_for_custom_field(name, "" if value is None else str(value))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not set %s on resource %s: %s", name, resource.id, exc)


# ---------------------------------------------------------------------------
# StormForge API
# ---------------------------------------------------------------------------

class StormForge:
    """The StormForge API as one org-level client-credentials client."""

    def __init__(self, api_host, client_id, client_secret):
        host = (api_host or "").strip()
        if not host:
            raise StormForgeError("No StormForge API host given.")
        if not host.startswith("http"):
            host = f"https://{host}"
        self.api = host.rstrip("/") + "/"
        self._client_id = client_id
        self._client_secret = client_secret
        self._metadata = None
        self._token = None
        self._token_expires = 0

    @classmethod
    def from_connection_info(cls):
        conn = ConnectionInfo.objects.filter(name=CONNECTION_INFO_NAME).first()
        if conn is None:
            raise StormForgeError(
                f"No connection info named '{CONNECTION_INFO_NAME}'. Create it in Admin > Connection Info "
                "with the StormForge client ID as Username and the client secret as Password."
            )
        if not conn.ip or not conn.username or not conn.password:
            raise StormForgeError(
                f"Connection info '{CONNECTION_INFO_NAME}' needs the StormForge API host (for example "
                "api.stormforge.io) as IP/Hostname, the client ID as Username and the client secret as Password."
            )
        return cls(conn.ip, conn.username, conn.password)

    @property
    def host(self):
        return urllib.parse.urlsplit(self.api).hostname or ""

    @property
    def ingest_url(self):
        """Where the agent's metrics forwarder writes for this endpoint.

        StormForge serves metrics ingest from the in. host beside the api.
        host; the chart's own default pairs https://in.stormforge.io/prometheus/write
        with https://api.stormforge.io/ (values.yaml forwarder.remoteWriteUrl).
        """
        if not self.host.startswith("api."):
            raise StormForgeError(
                f"Cannot tell where StormForge receives metrics for API host {self.host}. "
                f"Set the IP/Hostname of connection info '{CONNECTION_INFO_NAME}' to the StormForge "
                "API host, which starts with 'api.'."
            )
        return f"https://in.{self.host[len('api.'):]}/prometheus/write"

    # -- authorization -------------------------------------------------------

    def metadata(self):
        """The authorization server metadata (issuer, token and registration endpoints)."""
        if self._metadata is None:
            url = urllib.parse.urljoin(self.api, ".well-known/openid-configuration")
            response = _http("GET", url)
            if response.status_code != 200:
                raise StormForgeError(f"StormForge discovery at {url} returned HTTP {response.status_code}.")
            self._metadata = response.json()
        return self._metadata

    @property
    def issuer(self):
        return self.metadata().get("issuer") or self.api

    def token(self):
        """A bearer token for the org credential, reused until shortly before it expires."""
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        token_url = self.metadata().get("token_endpoint") or urllib.parse.urljoin(self.api, "oauth/token")
        # StormForge grants a client-credentials token only the scopes it asks
        # for; without a scope parameter the token carries none and every API
        # call answers 401. https://www.rfc-editor.org/rfc/rfc6749#section-4.4.2
        response = _http("POST", token_url, data={
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "audience": self.api,
            "scope": " ".join(ORG_SCOPES),
        })
        if response.status_code != 200:
            raise StormForgeError(
                f"StormForge refused the '{CONNECTION_INFO_NAME}' credential "
                f"(HTTP {response.status_code}): {_error_text(response)}. It must be allowed the "
                f"scopes {' '.join(ORG_SCOPES)}."
            )
        body = response.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in") or 300)
        return self._token

    def _call(self, method, url, ok=(200,), allow_404=False, bearer=None, **kwargs):
        headers = {"Authorization": f"Bearer {bearer or self.token()}", "Accept": "application/json"}
        response = _http(method, url, headers=headers, **kwargs)
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code not in ok:
            path = urllib.parse.urlparse(url).path
            hint = ""
            if response.status_code in (401, 403):
                hint = (f" The '{CONNECTION_INFO_NAME}' credential may lack one of the scopes "
                        f"{' '.join(ORG_SCOPES)}.")
            raise StormForgeError(
                f"StormForge {method} {path} returned HTTP {response.status_code}: "
                f"{_error_text(response)}.{hint}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    # -- clusters ------------------------------------------------------------

    def _cluster_url(self, name):
        return urllib.parse.urljoin(self.api, f"v3/clusters/{urllib.parse.quote(name)}")

    def get_cluster(self, name):
        """The StormForge cluster record, or None when StormForge has no such cluster."""
        return self._call("GET", self._cluster_url(name), allow_404=True)

    def delete_cluster(self, name):
        """Delete the cluster in StormForge. Returns False when it did not exist."""
        return self._call("DELETE", self._cluster_url(name), ok=(200, 202, 204), allow_404=True) is not None

    # -- cluster credentials (OAuth 2.0 dynamic client registration) ---------

    def _registration_endpoint(self):
        endpoint = self.metadata().get("registration_endpoint")
        if not endpoint:
            raise StormForgeError("StormForge discovery lists no registration_endpoint for credentials.")
        return endpoint

    def register_client(self, name):
        """A new client-credentials credential named ``name``, as StormForge returns it
        (client_id, client_secret, registration_client_uri, ...)."""
        info = self._call("POST", self._registration_endpoint(), ok=(200, 201), json={
            "client_name": name,
            "grant_types": ["client_credentials"],
        })
        if not info.get("client_id") or not info.get("client_secret"):
            raise StormForgeError("StormForge registered a credential but returned no client ID or secret.")
        return info

    def revoke_client(self, client_id):
        """Delete a credential this module registered. Returns False when it was already gone."""
        endpoint = self._registration_endpoint().rstrip("/") + "/"
        client_url = urllib.parse.urljoin(endpoint, urllib.parse.quote(client_id))
        info = self._call("GET", client_url, allow_404=True)
        if info is None:
            return False
        delete_url = urllib.parse.urljoin(endpoint, info.get("registration_client_uri") or client_url)
        self._call("DELETE", delete_url, ok=(200, 202, 204), allow_404=True,
                   bearer=info.get("registration_access_token"))
        return True


# ---------------------------------------------------------------------------
# Chart versions (anonymous OCI registry reads)
# https://distribution.github.io/distribution/spec/api/#listing-image-tags
# https://distribution.github.io/distribution/spec/auth/token/
# ---------------------------------------------------------------------------

def chart_versions(limit=None):
    """Released StormForge chart versions, newest first (pre-releases left out)."""
    tags_url = f"{CHART_REGISTRY}/v2/{CHART_REPOSITORY}/tags/list"
    response = _http("GET", tags_url, params={"n": 1000})
    if response.status_code == 401:
        challenge = response.headers.get("WWW-Authenticate", "")
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if not params.get("realm"):
            raise StormForgeError("registry.stormforge.io asked for a login it did not describe.")
        token_response = _http("GET", params["realm"], params={
            "service": params.get("service", ""),
            "scope": f"repository:{CHART_REPOSITORY}:pull",
        })
        if token_response.status_code != 200:
            raise StormForgeError(f"registry.stormforge.io token request returned HTTP {token_response.status_code}.")
        body = token_response.json()
        token = body.get("token") or body.get("access_token")
        response = _http("GET", tags_url, params={"n": 1000}, headers={"Authorization": f"Bearer {token}"})
    if response.status_code != 200:
        raise StormForgeError(f"Listing StormForge chart versions returned HTTP {response.status_code}.")
    versions = sorted({tag for tag in response.json().get("tags") or [] if _stable(tag)},
                      key=_version_key, reverse=True)
    if not versions:
        raise StormForgeError("registry.stormforge.io lists no released StormForge chart versions.")
    return versions[:limit] if limit else versions


# ---------------------------------------------------------------------------
# Kubernetes API of a CloudBolt-built cluster
# ---------------------------------------------------------------------------

class Kube:
    """The cluster's Kubernetes API with the credentials its container orchestrator holds.

    EKS and AKS go through the orchestrator's API wrapper (endpoint, EKS
    token or AKS admin certificate, CloudBolt SSL settings). GKE goes through
    gcp_gke.KubeClient, which trusts the cluster's own CA.
    """

    def __init__(self, kind, provider, orchestrator):
        self.kind = kind
        self.provider = provider
        self.orchestrator = orchestrator
        self._gke = None
        self._wrapper = None
        if kind == "GKE":
            from shared_modules.gcp_gke import KubeClient

            self._gke = KubeClient.from_orchestrator(orchestrator)
        else:
            self._wrapper = orchestrator.get_api_wrapper()
            base = self._wrapper.get_clusterrole_url("v1", skip_base_url_version=True)
            self._root = base[: -len("/api")] if base.endswith("/api") else base

    @classmethod
    def for_resource(cls, resource):
        from containerorchestrators.kuberneteshandler.models import Kubernetes

        kind, provider = cluster_kind(resource)
        orchestrator_id = resource.get_cf_values_as_dict().get(CLUSTER_ORCHESTRATOR_FIELD)
        orchestrator = None
        if orchestrator_id not in (None, ""):
            orchestrator = Kubernetes.objects.filter(id=int(str(orchestrator_id).strip())).first()
        if orchestrator is None:
            raise StormForgeError(
                f"Cluster '{resource.name}' has no CloudBolt container orchestrator "
                f"({CLUSTER_ORCHESTRATOR_FIELD}), so CloudBolt cannot reach its Kubernetes API."
            )
        try:
            return cls(kind, provider, orchestrator)
        except StormForgeError:
            raise
        except Exception as exc:
            raise StormForgeError(f"Could not open the Kubernetes API of '{resource.name}': {exc}")

    def request(self, method, path, body=None, params=None, allow_404=False, text=False):
        """One call; parsed JSON (or the body text when ``text``), None on 404 when ``allow_404``."""
        if self._gke is not None:
            from shared_modules.gcp_gke import GKEError

            try:
                result = self._gke.request(method, path, json_body=body, params=params,
                                           allow_404=allow_404, ok=(200, 201, 202))
            except GKEError as exc:
                raise StormForgeError(str(exc))
            if text and isinstance(result, dict) and set(result) == {"text"}:
                return result["text"]
            if text and result is not None and not isinstance(result, str):
                return json.dumps(result)
            return result
        url = f"{self._root}/{path.lstrip('/')}"
        kwargs = {"expected_statuses": [200, 201, 202, 204] + ([404] if allow_404 else [])}
        if body is not None:
            kwargs["json"] = body
        if params is not None:
            kwargs["params"] = params
        try:
            response = getattr(self._wrapper, f"requests_{method.lower()}")(url, **kwargs)
        except CloudBoltException as exc:
            raise StormForgeError(str(exc))
        if response.status_code == 404:
            return None
        if text:
            return response.text
        if response.text and response.headers.get("Content-Type", "").startswith("application/json"):
            return response.json()
        return {}

    def get(self, path, params=None):
        return self.request("GET", path, params=params, allow_404=True)

    def apply(self, collection, name, body):
        """Create ``body``, or replace the object named ``name`` (a PUT needs its resourceVersion)."""
        existing = self.get(f"{collection}/{name}")
        if existing is None:
            return self.request("POST", collection, body=body)
        body = json.loads(json.dumps(body))
        body.setdefault("metadata", {})["resourceVersion"] = existing["metadata"]["resourceVersion"]
        return self.request("PUT", f"{collection}/{name}", body=body)

    def delete(self, path, params=None):
        """Delete an object; returns False when it was already gone."""
        return self.request("DELETE", path, params=params, allow_404=True) is not None


def cluster_kind(resource):
    """('EKS'|'AKS'|'GKE', chart clusterProvider) from the field the cluster's build stores."""
    values = resource.get_cf_values_as_dict()
    for field, kind, provider in PROVIDERS:
        if values.get(field):
            return kind, provider
    raise StormForgeError(
        f"'{resource.name}' is not a cluster built or discovered by the AWS EKS, Azure Kubernetes "
        "Service or Google Kubernetes Engine blueprint."
    )


def cloud_cluster_name(resource):
    values = resource.get_cf_values_as_dict()
    for field, _kind, _provider in PROVIDERS:
        if values.get(field):
            return str(values[field])
    return resource.name


# ---------------------------------------------------------------------------
# Helm in a Job
# ---------------------------------------------------------------------------

def _secret_value(secret, key):
    raw = ((secret or {}).get("data") or {}).get(key)
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def deployed_release(kube):
    """The deployed StormForge Helm release record as a dict, or None when there is none.

    Helm keeps each release revision in a Secret labeled owner=helm,
    name=<release>, status=<status>, version=<revision>; its 'release' key is
    a base64, gzipped JSON document holding chart.metadata.version and the
    user-supplied values under 'config'.
    https://helm.sh/docs/topics/advanced/#storage-backends
    """
    listing = kube.get(f"/api/v1/namespaces/{NAMESPACE}/secrets",
                       params={"labelSelector": f"owner=helm,name={RELEASE},status=deployed"}) or {}
    items = listing.get("items") or []
    if not items:
        return None
    latest = max(items, key=lambda s: int(((s.get("metadata") or {}).get("labels") or {}).get("version") or 0))
    try:
        payload = base64.b64decode(base64.b64decode(latest["data"]["release"]))
        if payload[:2] == b"\x1f\x8b":
            payload = gzip.decompress(payload)
        return json.loads(payload)
    except (KeyError, ValueError, OSError) as exc:
        logger.warning("Could not read the StormForge Helm release record: %s", exc)
        return None


def installed_version(kube, release=None):
    """The chart version of the deployed StormForge release, or None when it is not installed."""
    release = release if release is not None else deployed_release(kube)
    if not release:
        return None
    return ((release.get("chart") or {}).get("metadata") or {}).get("version")


def _labels(extra=None):
    labels = dict(MANAGED_LABELS)
    labels["app.kubernetes.io/name"] = INSTALLER
    labels.update(extra or {})
    return labels


def _ensure_namespace(kube):
    if kube.get(f"/api/v1/namespaces/{NAMESPACE}") is None:
        kube.request("POST", "/api/v1/namespaces", body={
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": NAMESPACE, "labels": dict(MANAGED_LABELS)},
        })


def _write_auth_secret(kube, sf, client_id, client_secret):
    """The stormforge-auth Secret the chart reads when createAuthSecret=false."""
    def b64(value):
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    kube.apply(f"/api/v1/namespaces/{NAMESPACE}/secrets", AUTH_SECRET, {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": AUTH_SECRET, "namespace": NAMESPACE, "labels": dict(MANAGED_LABELS)},
        "data": {
            "STORMFORGE_CLIENT_ID": b64(client_id),
            "STORMFORGE_CLIENT_SECRET": b64(client_secret),
            "STORMFORGE_ISSUER": b64(sf.issuer),
            "STORMFORGE_SERVER": b64(sf.api),
        },
    })


def _job_spec(name, image, version, extra_env):
    env = [
        {"name": "HOME", "value": "/tmp"},
        {"name": "HELM_CACHE_HOME", "value": "/tmp/helm/cache"},
        {"name": "HELM_CONFIG_HOME", "value": "/tmp/helm/config"},
        {"name": "HELM_DATA_HOME", "value": "/tmp/helm/data"},
    ] + extra_env
    args = [
        "upgrade", "--install", RELEASE, CHART,
        "--version", version,
        "--namespace", NAMESPACE,
        "--values", "/values/values.yaml",
        "--reset-values",
        "--wait", "--timeout", HELM_TIMEOUT,
        "--rollback-on-failure",
    ]
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": _labels()},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": JOB_DEADLINE_SECONDS,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": _labels()},
                "spec": {
                    "serviceAccountName": INSTALLER,
                    "restartPolicy": "Never",
                    "nodeSelector": {"kubernetes.io/os": "linux"},
                    "securityContext": {
                        "runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [{
                        "name": "helm",
                        "image": image,
                        "args": args,
                        "env": env,
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "500m", "memory": "512Mi"},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": [
                            {"name": "tmp", "mountPath": "/tmp"},
                            {"name": "values", "mountPath": "/values", "readOnly": True},
                        ],
                    }],
                    "volumes": [
                        {"name": "tmp", "emptyDir": {}},
                        {"name": "values", "configMap": {"name": name}},
                    ],
                },
            },
        },
    }


def _pod(kube, job_name):
    pods = kube.get(f"/api/v1/namespaces/{NAMESPACE}/pods",
                    params={"labelSelector": f"job-name={job_name}"}) or {}
    items = pods.get("items") or []
    return items[0] if items else None


def _pod_problem(pod):
    """A container waiting reason that will not clear on its own, or why the pod is unscheduled."""
    status = (pod or {}).get("status") or {}
    for container in status.get("containerStatuses") or []:
        waiting = (container.get("state") or {}).get("waiting") or {}
        if waiting.get("reason") in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName",
                                     "CreateContainerConfigError", "CreateContainerError"):
            return f"{waiting['reason']}: {waiting.get('message', '')}".strip()
    for condition in status.get("conditions") or []:
        if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
            return f"{condition.get('reason', 'Unschedulable')}: {condition.get('message', '')}".strip()
    return ""


def run_helm(kube, values, version, image, extra_env, progress=set_progress):
    """Run `helm upgrade --install` in a Job; returns (succeeded, helm output or the reason it failed)."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    name = f"{INSTALLER}-{stamp}"
    sa_path = f"/api/v1/namespaces/{NAMESPACE}/serviceaccounts"
    crb_collection = "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings"
    cm_path = f"/api/v1/namespaces/{NAMESPACE}/configmaps"
    job_path = f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs"

    progress(f"Starting Helm ({image.split('@')[0]}) in namespace {NAMESPACE}")
    try:
        kube.apply(sa_path, INSTALLER, {
            "apiVersion": "v1", "kind": "ServiceAccount",
            "metadata": {"name": INSTALLER, "namespace": NAMESPACE, "labels": _labels()},
        })
        kube.apply(crb_collection, INSTALLER, {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
            "metadata": {"name": INSTALLER, "labels": _labels()},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
            "subjects": [{"kind": "ServiceAccount", "name": INSTALLER, "namespace": NAMESPACE}],
        })
        kube.request("POST", cm_path, body={
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": _labels()},
            "data": {"values.yaml": json.dumps(values, indent=2)},
        })
        kube.request("POST", job_path, body=_job_spec(name, image, version, extra_env))
        succeeded, detail = _wait_for_job(kube, name, progress)
        pod = _pod(kube, name)
        output = ""
        if pod is not None:
            output = kube.request("GET", f"/api/v1/namespaces/{NAMESPACE}/pods/{pod['metadata']['name']}/log",
                                  allow_404=True, text=True) or ""
        if succeeded:
            return True, output.strip()
        return False, "\n".join(part for part in (detail, output.strip()) if part)
    finally:
        for path, params in (
            (f"{job_path}/{name}", {"propagationPolicy": "Background"}),
            (f"{cm_path}/{name}", None),
            (f"{crb_collection}/{INSTALLER}", None),
            (f"{sa_path}/{INSTALLER}", None),
        ):
            try:
                kube.delete(path, params=params)
            except StormForgeError as exc:
                logger.warning("Could not clean up %s: %s", path, exc)


def _wait_for_job(kube, name, progress):
    deadline = time.time() + JOB_DEADLINE_SECONDS + 60
    stuck_since = None
    while True:
        job = kube.get(f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs/{name}") or {}
        status = job.get("status") or {}
        if status.get("succeeded"):
            return True, ""
        for condition in status.get("conditions") or []:
            if condition.get("type") == "Failed" and condition.get("status") == "True":
                return False, f"Helm job failed ({condition.get('reason', '')}: {condition.get('message', '')})."
        if status.get("failed"):
            return False, "Helm exited with an error."
        problem = _pod_problem(_pod(kube, name))
        if problem:
            stuck_since = stuck_since or time.time()
            if time.time() - stuck_since > IMAGE_FAILURE_GRACE_SECONDS:
                return False, f"The Helm pod cannot start: {problem}"
        else:
            stuck_since = None
        if time.time() > deadline:
            return False, f"Helm did not finish within {JOB_DEADLINE_SECONDS // 60} minutes."
        progress("Waiting for Helm to install the StormForge agent" + (f" ({problem})" if problem else ""))
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _check_identity(sf, kube, name):
    """Refuse to install over a different cluster StormForge already knows by this name."""
    record = sf.get_cluster(name)
    if record is None:
        return
    theirs = (record.get("kubernetes") or {}).get("systemNamespaceUID")
    ours = ((kube.get("/api/v1/namespaces/kube-system") or {}).get("metadata") or {}).get("uid")
    if theirs and ours and theirs != ours:
        raise StormForgeError(
            f"StormForge already has a different cluster named '{name}'. Delete it in StormForge "
            "or rename one of the clusters, then run Install or Upgrade StormForge Agent."
        )


def _ready_nodes(kube):
    """How many nodes can run a pod: Ready and not cordoned.

    https://kubernetes.io/docs/reference/node/node-status/#condition
    """
    count = 0
    for node in (kube.get("/api/v1/nodes") or {}).get("items") or []:
        if (node.get("spec") or {}).get("unschedulable"):
            continue
        conditions = (node.get("status") or {}).get("conditions") or []
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            count += 1
    return count


def _helm_managed(obj):
    metadata = (obj or {}).get("metadata") or {}
    return ((metadata.get("labels") or {}).get("app.kubernetes.io/managed-by") == "Helm"
            or "meta.helm.sh/release-name" in (metadata.get("annotations") or {}))


def _ensure_credential(sf, kube, resource, name, progress):
    """The chart values that point the agent at its credential.

    Reuses the cluster's stormforge-auth Secret when it holds one, otherwise
    registers a credential and writes the Secret. A Secret the chart itself
    created (StormForge installed by hand with the default createAuthSecret)
    stays chart-managed: switching to createAuthSecret=false would make Helm
    delete it during the upgrade, so its values are handed back to the chart.
    """
    secret = kube.get(f"/api/v1/namespaces/{NAMESPACE}/secrets/{AUTH_SECRET}")
    client_id = _secret_value(secret, "STORMFORGE_CLIENT_ID")
    client_secret = _secret_value(secret, "STORMFORGE_CLIENT_SECRET")
    if client_id and client_secret:
        progress(f"Reusing the StormForge credential in Secret {NAMESPACE}/{AUTH_SECRET}")
        if _helm_managed(secret):
            return {"createAuthSecret": True, "authorization": {
                "clientID": client_id, "clientSecret": client_secret, "issuer": sf.issuer,
            }}
        return {"createAuthSecret": False, "authorization": {"issuer": sf.issuer}}
    progress(f"Registering a StormForge credential for cluster {name}")
    info = sf.register_client(name)
    _write_auth_secret(kube, sf, info["client_id"], info["client_secret"])
    previous = resource.get_cf_values_as_dict().get(FIELD_CLIENT)
    _set_fields(resource, **{FIELD_CLIENT: info["client_id"]})
    if previous and previous != info["client_id"]:
        try:
            sf.revoke_client(previous)
        except StormForgeError as exc:
            logger.warning("Could not revoke the replaced StormForge credential %s: %s", previous, exc)
    # The forwarder takes its token URL from authorization.issuer even when the Secret is ours.
    return {"createAuthSecret": False, "authorization": {"issuer": sf.issuer}}


def _wait_for_connection(sf, name, progress):
    """(connected, agent version StormForge reports) once the agent has checked in."""
    deadline = time.time() + CONNECT_WAIT_SECONDS
    while True:
        record = sf.get_cluster(name)
        if record is not None and (record.get("status") or {}).get("phase") != "Inactive":
            products = (record.get("stormforge") or {}).get("products") or []
            agent = next((p.get("version") for p in products if p.get("name") == "stormforge-agent"), None)
            return True, agent
        if time.time() > deadline:
            return False, None
        progress("Waiting for the StormForge agent to check in with StormForge")
        time.sleep(10)


def install_or_upgrade(resource, version=None, progress=set_progress):
    """Install or upgrade the StormForge agent; returns (status, output, error)."""
    ensure_custom_fields()
    sf = StormForge.from_connection_info()
    kube = Kube.for_resource(resource)

    target = version or chart_versions(limit=1)[0]
    if not _stable(target):
        raise StormForgeError(f"'{target}' is not a released StormForge chart version.")
    release = deployed_release(kube)
    current = installed_version(kube, release)
    # Keep the name an existing install registered under, so StormForge's history carries over.
    name = (resource.get_cf_values_as_dict().get(FIELD_CLUSTER)
            or ((release or {}).get("config") or {}).get("clusterName")
            or cluster_name_for_stormforge(cloud_cluster_name(resource)))

    ingest_url = sf.ingest_url
    _check_identity(sf, kube, name)
    # Helm and the agent run as pods; a cluster built without nodes (an EKS
    # cluster before its first node group) has nowhere to run them. Checked
    # after the StormForge credential is proven and before anything is
    # registered or written to the cluster.
    if not _ready_nodes(kube):
        raise StormForgeError(
            f"{kube.kind} cluster {cloud_cluster_name(resource)} has no ready nodes to run the "
            "StormForge agent. Add a node group or node pool first."
        )
    _ensure_namespace(kube)
    values = _ensure_credential(sf, kube, resource, name, progress)
    _set_fields(resource, **{FIELD_CLUSTER: name})

    values.update({
        "clusterName": name,
        "clusterProvider": kube.provider,
        "clusterLabels": {"cloudbolt.io/resource-id": str(resource.id)},
        # The chart defaults both to the production endpoint; use the connection info's.
        "stormforge": {"address": sf.api},
        "forwarder": {"remoteWriteUrl": ingest_url},
    })
    proxy_url = _environment_setting(resource, ENV_PROXY_URL)
    no_proxy = _environment_setting(resource, ENV_NO_PROXY)
    extra_env = []
    if proxy_url:
        values["proxyUrl"] = proxy_url
        # Helm itself pulls the chart from registry.stormforge.io through the same proxy.
        extra_env += [{"name": "HTTPS_PROXY", "value": proxy_url}]
    if no_proxy:
        values["noProxy"] = no_proxy
        extra_env += [{"name": "NO_PROXY", "value": no_proxy}]
    image = _environment_setting(resource, ENV_HELM_IMAGE) or DEFAULT_HELM_IMAGE

    verb = "Installing" if current is None else ("Reinstalling" if current == target else "Upgrading")
    change = target if current is None else f"{current} -> {target}"
    progress(f"{verb} StormForge agent {change} on {kube.kind} cluster {name}")
    succeeded, output = run_helm(kube, values, target, image, extra_env, progress)
    if output:
        logger.info("Helm output:\n%s", output)
    if not succeeded:
        _set_fields(resource, **{FIELD_STATUS: f"Install of {target} failed"})
        return "FAILURE", "", f"Helm could not install StormForge {target}: {output}"

    _set_fields(resource, **{FIELD_VERSION: target})
    connected, agent_version = _wait_for_connection(sf, name, progress)
    if not connected:
        _set_fields(resource, **{FIELD_STATUS: f"{target} installed; agent not connected"})
        return ("WARNING", f"StormForge agent {change} is installed on {name}.",
                "StormForge has not heard from the agent yet. Check that the cluster can reach "
                f"{sf.host}, {urllib.parse.urlsplit(ingest_url).hostname}, registry.stormforge.io "
                "and quay.io on port 443.")
    _set_fields(resource, **{FIELD_STATUS: "Connected"})
    reported = f" (StormForge reports agent {agent_version})" if agent_version else ""
    return "SUCCESS", f"StormForge agent {change} is installed on {name} and connected{reported}.", ""


def remove(resource, progress=set_progress):
    """Revoke the cluster's credential and delete it in StormForge; returns (status, output, error)."""
    values = resource.get_cf_values_as_dict()
    name = values.get(FIELD_CLUSTER)
    client_id = values.get(FIELD_CLIENT)
    if not name and not client_id:
        return "SUCCESS", "StormForge was never installed on this cluster by CloudBolt; nothing to remove.", ""
    sf = StormForge.from_connection_info()
    done = []
    if client_id:
        progress(f"Revoking StormForge credential {client_id}")
        if sf.revoke_client(client_id):
            done.append(f"revoked credential {client_id}")
    if name:
        progress(f"Deleting cluster {name} in StormForge")
        if sf.delete_cluster(name):
            done.append(f"deleted cluster {name}")
    _set_fields(resource, **{FIELD_STATUS: "Removed from StormForge"})
    if not done:
        return "SUCCESS", "StormForge had no record of this cluster or its credential.", ""
    return "SUCCESS", "StormForge: " + " and ".join(done) + ".", ""


def version_options(resource):
    """[(version, label)] for the action: what is installed and what each choice changes it to."""
    versions = chart_versions(limit=8)
    latest = versions[0]
    try:
        current = installed_version(Kube.for_resource(resource))
    except StormForgeError as exc:
        logger.warning("Could not read the installed StormForge version: %s", exc)
        current = resource.get_cf_values_as_dict().get(FIELD_VERSION) or None
    options = []
    for version in versions:
        suffix = " (latest)" if version == latest else ""
        if current is None:
            label = f"Install {version}{suffix}"
        elif version == current:
            label = f"Reinstall {version} (installed)"
        elif _stable(current) and _version_key(version) > _version_key(current):
            label = f"Upgrade {current} → {version}{suffix}"
        else:
            label = f"Downgrade {current} → {version}"
        options.append((version, label))
    return options


# ---------------------------------------------------------------------------
# Entry points for the blueprint items
# ---------------------------------------------------------------------------

def run_build_step(job, resource, kwargs):
    """Blueprint build item: install the agent; a failure is a warning so the order completes."""
    resource = _resolve_resource(job, resource, kwargs)
    if resource is None:
        return "WARNING", "", "StormForge was not installed: no cluster resource was supplied."
    retry = "Run Install or Upgrade StormForge Agent on the cluster to try again."
    try:
        status, output, error = install_or_upgrade(resource)
    except StormForgeError as exc:
        _set_fields(resource, **{FIELD_STATUS: "Not installed"})
        return "WARNING", "", f"StormForge was not installed: {exc} {retry}"
    except Exception as exc:  # noqa: BLE001 - the cluster itself is fine; never fail its order here
        logger.exception("StormForge install failed for resource %s", resource.id)
        _set_fields(resource, **{FIELD_STATUS: "Not installed"})
        return "WARNING", "", f"StormForge was not installed: {exc} {retry}"
    if status == "FAILURE":
        return "WARNING", output, f"{error} {retry}"
    return status, output, error


def run_teardown_step(job, resource, kwargs):
    """Blueprint teardown item, after the cluster is deleted: remove it from StormForge."""
    resource = _resolve_resource(job, resource, kwargs)
    if resource is None:
        return "WARNING", "", "No cluster resource was supplied, so nothing was removed from StormForge."
    try:
        return remove(resource)
    except Exception as exc:  # noqa: BLE001 - the cluster is already gone; report and move on
        logger.exception("StormForge removal failed for resource %s", resource.id)
        return "WARNING", "", (
            f"The cluster was deleted, but removing it from StormForge failed: {exc} "
            "Delete it in the StormForge app."
        )
