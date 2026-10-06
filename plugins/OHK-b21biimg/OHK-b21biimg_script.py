"""
Bicep Engine Connectivity Check

Probes every external host the Bicep deployment engine depends on and reports,
per host: which CA actually signed the certificate the appliance receives,
whether the request works under CloudBolt's current SSL setting, and, when the
global SSL-verification preference is OFF, whether it WOULD work with
verification on. That last column tells an admin what switching verification
back on will break and which CA to upload first. Behind an SSL-inspecting
proxy every inspected host shows the proxy's CA as issuer; upload that CA at
Admin > SSL Certificates and re-run until each host is OK with verification on.

Read-only: anonymous requests only, nothing stored. Run it from Admin >
Recurring Jobs > Bicep Engine Connectivity Check > Run Now and read the job's
progress output. The same diagnosis is embedded in the engine's own errors,
so a failed order names the host and CA without this job.
"""
import os

import certifi
import requests

from common.methods import set_progress
from utilities.helpers import get_ssl_verification
from utilities.logger import ThreadLogger

from shared_modules.bicep_engine import (
    ARM_BASE,
    AZURE_LOGIN_BASE,
    BICEP_DOWNLOAD_URL_TEMPLATE,
    BICEP_VERSION,
    HTTP_TIMEOUT_S,
    describe_tls_failure,
    tls_presented_chain,
    tls_trust_store,
)
from shared_modules.github import GITHUB_API_BASE

logger = ThreadLogger(__name__)

# (label, url, why the engine needs it). Anonymous requests: any HTTP status,
# including 401/403, proves routing and TLS work; only transport errors fail.
PROBES = [
    ("GitHub web", "https://github.com/",
     "origin of the Bicep binary download redirect"),
    ("GitHub API", f"{GITHUB_API_BASE}/",
     "template fetch: directory listing, file and archive download"),
    ("Bicep release asset", BICEP_DOWNLOAD_URL_TEMPLATE.format(version=BICEP_VERSION),
     "the compiler binary; redirects to a GitHub asset CDN host"),
    # OpenID discovery document, anonymous:
    # https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc#fetch-the-openid-configuration-document
    ("Azure login", f"{AZURE_LOGIN_BASE}/common/v2.0/.well-known/openid-configuration",
     "service-principal token"),
    # Subscriptions - List; unauthenticated returns 401, which is a pass here:
    # https://learn.microsoft.com/en-us/rest/api/resources/subscriptions/list
    ("Azure Resource Manager", f"{ARM_BASE}/subscriptions?api-version=2022-12-01",
     "what-if previews and deployment stacks"),
    ("Bicep module registry (optional)", "https://mcr.microsoft.com/v2/",
     "only templates that use br: registry modules"),
]


def _hostname(url):
    return url.split("/")[2]


def _probe(url, verify):
    """One GET with the given `verify`; returns (ok, detail). Streams and
    closes immediately so the 99 MB binary probe transfers only headers."""
    try:
        resp = requests.get(url, stream=True, timeout=HTTP_TIMEOUT_S,
                            verify=verify, allow_redirects=True)
        final_host = _hostname(resp.url)  # host only: a redirected asset URL carries a signed query
        resp.close()
        return True, f"HTTP {resp.status_code} from {final_host}"
    except requests.exceptions.SSLError as e:
        return False, describe_tls_failure(url, e, verify=verify)
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {str(e)[:300]}"


def _forced_bundle():
    """The bundle verification WOULD use if the global preference were on:
    CloudBolt's generated PEM (certifi + uploaded CAs) when it exists, else
    certifi alone."""
    try:
        from utilities.helpers import GENERATED_PEM_FILE
        if os.path.isfile(GENERATED_PEM_FILE):
            return GENERATED_PEM_FILE
    except ImportError:
        pass
    return certifi.where()


def run(job, *args, **kwargs):
    current = get_ssl_verification()
    store, count, uploaded = tls_trust_store(current)
    if store == "off":
        set_progress("CloudBolt global SSL verification: OFF (requests accept any certificate).")
        forced = _forced_bundle()
        set_progress(f"Also testing each host with verification ON using {forced}.")
    else:
        set_progress(f"CloudBolt global SSL verification: ON, bundle {store} ({count} CAs).")
        forced = None
    set_progress(f"CAs uploaded at Admin > SSL Certificates: {'; '.join(uploaded) or 'none'}")

    failures, would_fail = [], []
    for label, url, why in PROBES:
        host = _hostname(url)
        set_progress(f"--- {label}: {host} ({why})")
        try:
            chain = tls_presented_chain(host)
            line = f"    certificate issued by: {chain[0][1]}"
            if len(chain) > 1 and chain[-1][1] != chain[0][1]:
                line += f"; root the trust store must hold: {chain[-1][1]}"
            set_progress(line)
        except Exception as e:  # noqa: BLE001 — diagnostics never fail the job
            set_progress(f"    could not read the presented certificate: {e}")

        ok, detail = _probe(url, current)
        set_progress(f"    with current setting: {'OK' if ok else 'FAILED'} - {detail}")
        if not ok:
            failures.append(label)

        if forced is not None:
            ok_on, detail_on = _probe(url, forced)
            set_progress(f"    with verification ON: {'OK' if ok_on else 'WOULD FAIL'} - {detail_on}")
            if not ok_on:
                would_fail.append(label)

    if failures:
        return ("FAILURE",
                f"Unreachable under the current SSL setting: {', '.join(failures)}. "
                f"See the progress output for the host, issuer, and fix.", "")
    if would_fail:
        return ("WARNING",
                f"All hosts reachable now, but turning SSL verification on would break: "
                f"{', '.join(would_fail)}. Upload the issuing CAs shown above at "
                f"Admin > SSL Certificates first.", "")
    return "SUCCESS", "All Bicep engine hosts are reachable under the current SSL setting.", ""
