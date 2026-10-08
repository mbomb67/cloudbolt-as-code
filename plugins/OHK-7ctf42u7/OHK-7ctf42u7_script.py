"""
URL Connectivity Check

Probes any URLs an administrator enters on the recurring job and reports,
per URL: which CA actually signed the certificate the appliance receives,
whether the request works under CloudBolt's current SSL setting, and, when
the global SSL-verification preference is OFF, whether it WOULD work with
verification on. That last column tells an admin what switching verification
back on will break and which CA to upload first. Behind an SSL-inspecting
proxy every inspected host shows the proxy's CA as issuer; upload that CA at
Admin > SSL Certificates and re-run until each URL is OK with verification on.

Read-only: anonymous requests only, nothing stored. Admin > Recurring Jobs >
URL Connectivity Check > Edit, enter the URLs, save, then Run Now. The job
page shows one pass/fail line per URL; the evidence (issuing CA, chain, what
the trust store holds, the fix) is written to the job log for the
administrator. Self-contained: no shared modules.
"""
import os
import re
import socket
import ssl
from urllib.parse import urlparse

import certifi
import requests

from common.methods import set_progress
from utilities.helpers import get_ssl_verification
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

HTTP_TIMEOUT_S = 60
TLS_PEEK_TIMEOUT_S = 15
_PEM_CERT_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)


# --- URL list -----------------------------------------------------------------

def _parse_urls(raw):
    """One URL per line; commas and whitespace also separate. Blank lines and
    lines starting with # are ignored. A bare host becomes https://host.
    Order is kept, duplicates dropped. Raises ValueError on a non-http(s) entry."""
    urls = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for token in re.split(r"[\s,]+", line):
            if not token:
                continue
            if "://" not in token:
                token = "https://" + token
            parsed = urlparse(token)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError(f"'{token}' is not an http(s) URL")
            if token not in urls:
                urls.append(token)
    return urls


# --- TLS diagnostics ----------------------------------------------------------

def _redact_url(error):
    """Strip the request URL from a requests error before it reaches a job
    log: a redirected URL can carry a signed query string."""
    return re.sub(r"with url: \S+", "with url: <redacted>", str(error))


def _tls_failed_host(url, error):
    """The host whose certificate failed. requests names it in the error,
    which is what matters after a redirect."""
    m = re.search(r"host='([^']+)'", str(error))
    return m.group(1) if m else (urlparse(url).hostname or url)


def tls_presented_chain(host, port=443):
    """
    [(subject, issuer), ...] for the certificate chain `host` actually sends
    the appliance, leaf first, read WITHOUT verifying it. The last issuer is
    the CA the client must trust. Needs `cryptography`, which CloudBolt ships.
    """
    from cryptography import x509
    ctx = ssl.create_default_context()
    # Diagnostic peek: hostname/chain checks are off on purpose (we are
    # reading what the server presents), but never negotiate below TLS 1.2.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=TLS_PEEK_TIMEOUT_S) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            certs = [x509.load_der_x509_certificate(
                tls.getpeercert(binary_form=True))]
            try:
                # CPython >= 3.10 exposes the unverified chain on the private
                # SSL object; the leaf alone is the fallback.
                chain = tls._sslobj.get_unverified_chain()  # noqa: SLF001
                if chain:
                    loaded = []
                    for c in chain:
                        pem = c.public_bytes()
                        pem = pem.encode() if isinstance(pem, str) else pem
                        loaded.append(x509.load_pem_x509_certificate(pem))
                    certs = loaded
            except Exception:  # noqa: BLE001 — best effort
                pass
    return [(c.subject.rfc4514_string(), c.issuer.rfc4514_string())
            for c in certs]


def tls_trust_store(verify=None):
    """
    What `requests` trusts right now: (bundle_path, cert_count, uploaded) where
    `uploaded` lists the subjects of the CAs an admin added at Admin > SSL
    Certificates. When the global SSL-verification preference is off the path
    is the string "off" and the count 0 (uploaded is still reported).
    """
    if verify is None:
        verify = get_ssl_verification()
    uploaded = []
    try:
        from cryptography import x509
        from utilities.models import RootCertificate
        for rc in RootCertificate.objects.active():
            cert = x509.load_pem_x509_certificate(rc.certificate.encode())
            uploaded.append(cert.subject.rfc4514_string())
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Could not list uploaded root certificates: {e}")
    if verify is False:
        return "off", 0, uploaded
    path = verify if isinstance(verify, str) else certifi.where()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fd:
            count = len(_PEM_CERT_RE.findall(fd.read()))
    except OSError:
        count = 0
    return path, count, uploaded


def _bundle_subjects(path):
    from cryptography import x509
    subjects = set()
    with open(path, "r", encoding="utf-8", errors="replace") as fd:
        for pem in _PEM_CERT_RE.findall(fd.read()):
            try:
                subjects.add(x509.load_pem_x509_certificate(
                    pem.encode()).subject.rfc4514_string())
            except Exception:  # noqa: BLE001
                continue
    return subjects


def describe_tls_failure(url, error, verify=None):
    """
    Operator-facing explanation of an SSL verification failure against `url`:
    the host, the CA that signed what the appliance received, whether that CA
    is in CloudBolt's trust store, and the fix. Never raises.
    """
    host = _tls_failed_host(url, error)
    lines = [f"TLS certificate verification failed for {host}."]
    chain = []
    try:
        chain = tls_presented_chain(host)
    except Exception as e:  # noqa: BLE001
        lines.append(f"Could not read the certificate {host} presents: {e}.")
    if chain:
        subject, issuer = chain[0]
        lines.append(f"Certificate received: {subject}; issued by: {issuer}.")
        if len(chain) > 1:
            lines.append("Chain sent: " + " -> ".join(s for s, _ in chain) + ".")
    store, count, uploaded = tls_trust_store(verify)
    shown = "; ".join(uploaded) or "none"
    if store == "off":
        lines.append("CloudBolt's global SSL verification is OFF, so this did "
                     f"not come from CloudBolt's trust settings (uploaded CAs: {shown}).")
    else:
        lines.append(f"CloudBolt trust store: {store} ({count} CAs; uploaded at "
                     f"Admin > SSL Certificates: {shown}).")
        if chain:
            needed = chain[-1][1]
            try:
                present = needed in _bundle_subjects(store)
            except Exception:  # noqa: BLE001
                present = None
            if present is False:
                lines.append(
                    f"The signing CA '{needed}' is NOT in the store. Upload it (and "
                    f"any intermediate between it and the certificate above) at "
                    f"Admin > SSL Certificates; the next run picks it up, no restart.")
            elif present:
                lines.append(
                    f"The signing CA '{needed}' IS in the store, so a missing CA is "
                    f"not the cause; check the error for a hostname mismatch or an "
                    f"expired certificate.")
    lines.append(f"Error: {_redact_url(error)}")
    return "\n".join(lines)


# --- probes -------------------------------------------------------------------

def _probe(url, verify):
    """One GET with the given `verify`; returns (ok, detail). Streams and
    closes immediately so only headers transfer, whatever the URL serves.
    Any HTTP status, including 401/403/404, proves routing and TLS work;
    only transport errors fail."""
    try:
        resp = requests.get(url, stream=True, timeout=HTTP_TIMEOUT_S,
                            verify=verify, allow_redirects=True)
        final_host = urlparse(resp.url).hostname  # host only: a redirect may carry a signed query
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
    raw = """{{ target_urls }}"""
    try:
        urls = _parse_urls(raw)
    except ValueError as e:
        return "FAILURE", f"URLs to check: {e}. Enter one http(s) URL per line.", ""
    if not urls:
        return ("FAILURE",
                "No URLs to check. Edit this recurring job, enter one URL per line "
                "under 'URLs to check', save, and click Run Now.", "")

    current = get_ssl_verification()
    store, count, uploaded = tls_trust_store(current)
    if store == "off":
        set_progress("CloudBolt global SSL verification is OFF; also testing each URL "
                     "with verification on. Details are in the job log.")
        forced = _forced_bundle()
        logger.info(f"Verification OFF: requests accept any certificate. Forced-on "
                    f"probes use {forced}.")
    else:
        set_progress("CloudBolt global SSL verification is ON. Details are in the job log.")
        forced = None
        logger.info(f"Verification ON: bundle {store} ({count} CAs).")
    logger.info(f"CAs uploaded at Admin > SSL Certificates: {'; '.join(uploaded) or 'none'}")
    logger.info(f"Checking {len(urls)} URL(s).")

    failures, would_fail = [], []
    for url in urls:
        parsed = urlparse(url)
        host = parsed.hostname
        logger.info(f"--- {url}")
        if parsed.scheme == "https":
            try:
                chain = tls_presented_chain(host, parsed.port or 443)
                line = f"certificate issued by: {chain[0][1]}"
                if len(chain) > 1 and chain[-1][1] != chain[0][1]:
                    line += f"; root the trust store must hold: {chain[-1][1]}"
                logger.info(line)
            except Exception as e:  # noqa: BLE001 — diagnostics never fail the job
                logger.info(f"could not read the presented certificate: {e}")
        else:
            logger.info("plain http: no certificate to inspect")

        ok, detail = _probe(url, current)
        logger.info(f"with current setting: {'OK' if ok else 'FAILED'} - {detail}")
        if not ok:
            failures.append(url)

        verdict = "OK" if ok else "FAILED (see job log)"
        if forced is not None and parsed.scheme == "https":
            ok_on, detail_on = _probe(url, forced)
            logger.info(f"with verification ON: {'OK' if ok_on else 'WOULD FAIL'} - {detail_on}")
            if not ok_on:
                would_fail.append(url)
                if ok:
                    verdict = "OK now, would fail with verification on (see job log)"
        set_progress(f"{url}: {verdict}")

    if failures:
        return ("FAILURE",
                f"Unreachable under the current SSL setting: {', '.join(failures)}. "
                f"The job log has the host, issuer, and fix.", "")
    if would_fail:
        return ("WARNING",
                f"All URLs reachable now, but turning SSL verification on would break: "
                f"{', '.join(would_fail)}. Upload the issuing CAs named in the job log "
                f"at Admin > SSL Certificates first.", "")
    return "SUCCESS", f"All {len(urls)} URL(s) reachable under the current SSL setting.", ""
