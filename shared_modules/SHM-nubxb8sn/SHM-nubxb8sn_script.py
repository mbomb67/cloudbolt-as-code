"""
CloudBolt shared module: ad-hoc certificate enrollment against a Microsoft AD CS
(Windows) Certificate Authority via its **Certification Authority Web Enrollment**
pages (``/certsrv/certfnsh.asp``), over HTTPS with Basic auth.

Imported by the "Request Certificate (Windows CA)" build plugin and the
"Retrieve Pending Certificate" day-2 plugin as::

    from shared_modules.windows_ca import (
        get_ca_client,
        ensure_custom_fields,
        generate_private_key_pem,
        build_csr_pem,
        cert_metadata,
        CertsrvError,
        CertsrvConfigError,
        CertificatePendingError,
        CONNECTION_INFO_NAME,
        DEFAULT_TEMPLATE,
        CF_ISSUED_CERT, CF_PRIVATE_KEY, CF_REQUEST_ID,
        CF_THUMBPRINT, CF_TEMPLATE, CF_COMMON_NAME, CF_STATUS,
    )

Layering (strict — keeps the CSR/HTTP client unit-testable off-platform):

- The CSR helpers, ``cert_metadata``, and ``WebEnrollClient`` carry ZERO CloudBolt
  references: plain strings plus the ``requests`` and ``cryptography`` libraries
  (both confirmed present on the appliance).
- ``get_ca_client()`` is the ONLY CloudBolt-importing seam: it resolves the
  ``ConnectionInfo`` named ``CONNECTION_INFO_NAME``, reads the (decrypted) service
  password into a local, and returns a ``WebEnrollClient``. The password and any
  generated/submitted private key are NEVER logged, returned in errors, or
  interpolated into log lines.

Mechanism / protocol — this drives the Web Enrollment UI, NOT a formal API. Cite
at the call site and validate against the real CA (see LAB VALIDATION below):
- AD CS Certification Authority Web Enrollment role:
  https://learn.microsoft.com/en-us/windows-server/identity/ad-cs/certificate-authority-web-enrollment
- Reference implementation of the certfnsh.asp / certnew.cer flow:
  magnuswatn/certsrv — https://github.com/magnuswatn/certsrv
- Operator + lab setup (recommended demo config, Basic-auth-over-HTTPS):
  docs/windows-ca-cert-request-setup.md

LAB VALIDATION REQUIRED: the certfnsh.asp form fields and response parsing below
follow the certsrv reference implementation but have NOT been run against a live
CA from this appliance. Verify §6 of the setup doc (the curl/openssl end-to-end)
before enabling in production. Basic-auth-over-HTTPS is a DEMO posture; move to
Windows Integrated / CES for production (setup doc §7).
"""
import re

import requests
from requests.auth import HTTPBasicAuth

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# ============================ OPERATOR CONFIG BLOCK ============================
# Primary, versioned configuration surface (config-as-code). A shared-module
# change requires a CloudBolt restart to take effect (the module is cached in the
# running process), not just a repo sync.

# Name of the ConnectionInfo that holds the CA endpoint + service-account creds.
# protocol=https, ip=<CA FQDN>, port=443, username=DOMAIN\\svc, password=<secret>.
CONNECTION_INFO_NAME = "Demo Windows CA"

# Certificate template the request enrolls against (Enterprise CA only; ignored by
# a Standalone CA). Overridable per-order via the certificate_template form input.
DEFAULT_TEMPLATE = "CloudBoltWebServer"

# RSA key size for CloudBolt-generated keys.
DEFAULT_KEY_SIZE = 2048

# TLS validation. With CA_CERTS_FILE set, the CA's IIS cert is validated against
# that bundle (recommended). With it empty, VERIFY_TLS controls verification;
# VERIFY_TLS=False is encrypt-only (lab self-signed CAs only — never production).
CA_CERTS_FILE = ""
VERIFY_TLS = False

# Bound so a hung CA cannot pin a jobengine worker.
HTTP_TIMEOUT_S = 60
# ==========================================================================

# Resource custom fields this content persists (created at runtime, not declared
# as order-form inputs).
CF_ISSUED_CERT = "windows_ca_issued_certificate"
CF_PRIVATE_KEY = "windows_ca_private_key"
CF_REQUEST_ID = "windows_ca_request_id"
CF_THUMBPRINT = "windows_ca_thumbprint"
CF_TEMPLATE = "windows_ca_template"
CF_COMMON_NAME = "windows_ca_common_name"
CF_STATUS = "windows_ca_status"

_REQID_RE = re.compile(r"certnew\.cer\?ReqID=(\d+)", re.IGNORECASE)
_REQID_ANY_RE = re.compile(r"ReqID=(\d+)", re.IGNORECASE)
_PENDING_MARKERS = (
    "taken under submission",
    "your certificate request has been received",
    "pending",
)
_DENIED_MARKERS = ("denied", "the disposition message is")


# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------
class CertsrvError(Exception):
    """Base error for Web Enrollment operations."""


class CertsrvConfigError(CertsrvError):
    """Misconfiguration (missing ConnectionInfo, blank password, bad input)."""


class CertificatePendingError(CertsrvError):
    """The CA accepted the request but it awaits manager approval.

    ``request_id`` is the CA Request ID to poll later via ``retrieve_pending``.
    """

    def __init__(self, request_id, message=None):
        self.request_id = request_id
        super().__init__(message or "Certificate request %s is pending approval." % request_id)


# ----------------------------------------------------------------------------
# Pure CSR / certificate helpers (no CloudBolt deps)
# ----------------------------------------------------------------------------
def generate_private_key_pem(key_size=DEFAULT_KEY_SIZE):
    """Generate a fresh RSA private key, returned as an unencrypted PKCS#8 PEM."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=int(key_size))
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def build_csr_pem(private_key_pem, common_name, sans=None):
    """Build a PEM CSR for ``common_name`` (+ optional DNS SANs) from a PEM key."""
    if not common_name:
        raise CertsrvConfigError("A Common Name is required to build a CSR.")
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    except Exception as exc:  # noqa: BLE001 - surface a clean message, not key bytes
        raise CertsrvConfigError("Could not load the provided private key (expected an unencrypted PEM).") from exc

    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    )
    dns_names = [s.strip() for s in (sans or []) if s and s.strip()]
    if dns_names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in dns_names]),
            critical=False,
        )
    csr = builder.sign(key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def cert_metadata(cert_pem):
    """Return {thumbprint, serial, subject, not_after} for an issued PEM cert."""
    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    return {
        "thumbprint": cert.fingerprint(hashes.SHA1()).hex(),
        "serial": format(cert.serial_number, "x"),
        "subject": cert.subject.rfc4514_string(),
        "not_after": not_after.isoformat(),
    }


def _looks_like_cert(text):
    return "-----BEGIN CERTIFICATE-----" in (text or "")


# ----------------------------------------------------------------------------
# Pure Web Enrollment client (no CloudBolt deps)
# ----------------------------------------------------------------------------
class WebEnrollClient:
    """Submits CSRs to the AD CS Web Enrollment pages and retrieves issued certs.

    Implements the small certfnsh.asp POST -> certnew.cer GET flow. ``verify`` is
    passed straight to ``requests`` (a CA-bundle path, True, or False).
    """

    def __init__(self, base_url, username, password, template_default=DEFAULT_TEMPLATE,
                 verify=False, timeout=HTTP_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self._auth = HTTPBasicAuth(username, password)
        self.template_default = template_default
        self.verify = verify
        self.timeout = timeout

    def _url(self, page):
        return "%s/certsrv/%s" % (self.base_url, page)

    def submit_csr(self, csr_pem, template=None):
        """Submit a PEM CSR. Returns {status, certificate, request_id}.

        status is 'issued' (certificate populated) or raises
        CertificatePendingError on manager-approval templates.
        """
        # certfnsh.asp form fields per the certsrv reference implementation
        # (https://github.com/magnuswatn/certsrv). CertAttrib carries the template.
        data = {
            "Mode": "newreq",
            "CertRequest": csr_pem,
            "CertAttrib": "CertificateTemplate:%s" % (template or self.template_default),
            "TargetStoreFlags": "0",
            "SaveCert": "yes",
            "ThumbPrint": "",
        }
        resp = requests.post(
            self._url("certfnsh.asp"), data=data, auth=self._auth,
            verify=self.verify, timeout=self.timeout,
        )
        resp.raise_for_status()
        body = resp.text or ""
        match = _REQID_RE.search(body)
        if match:
            req_id = int(match.group(1))
            return {"status": "issued", "certificate": self.retrieve_cert(req_id), "request_id": req_id}

        low = body.lower()
        if any(m in low for m in _PENDING_MARKERS):
            pend = _REQID_ANY_RE.search(body)
            req_id = int(pend.group(1)) if pend else None
            raise CertificatePendingError(req_id)
        if any(m in low for m in _DENIED_MARKERS):
            raise CertsrvError("The CA denied or could not process the request (check template name and Enroll permission).")
        raise CertsrvError("Unexpected Web Enrollment response (no Request ID found). Verify the /certsrv pages and Basic auth.")

    def retrieve_cert(self, request_id):
        """GET an issued certificate by Request ID as base64 PEM."""
        resp = requests.get(
            self._url("certnew.cer"), params={"ReqID": request_id, "Enc": "b64"},
            auth=self._auth, verify=self.verify, timeout=self.timeout,
        )
        resp.raise_for_status()
        if not _looks_like_cert(resp.text):
            raise CertsrvError("Request %s did not return an issued certificate (still pending or denied)." % request_id)
        return resp.text

    def retrieve_pending(self, request_id):
        """Poll a previously pending request. Returns {status, certificate}."""
        resp = requests.get(
            self._url("certnew.cer"), params={"ReqID": request_id, "Enc": "b64"},
            auth=self._auth, verify=self.verify, timeout=self.timeout,
        )
        resp.raise_for_status()
        if _looks_like_cert(resp.text):
            return {"status": "issued", "certificate": resp.text}
        return {"status": "pending", "certificate": None}

    def get_chain(self):
        """GET the CA certificate chain as a base64 PKCS#7 (.p7b)."""
        resp = requests.get(
            self._url("certnew.p7b"), params={"ReqID": "CACert", "Renewal": "0", "Enc": "b64"},
            auth=self._auth, verify=self.verify, timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.text


# ----------------------------------------------------------------------------
# CloudBolt seam (the ONLY CloudBolt-importing code)
# ----------------------------------------------------------------------------
def get_ca_client():
    """Resolve the ConnectionInfo named CONNECTION_INFO_NAME into a WebEnrollClient.

    Raises CertsrvConfigError with an operator-actionable message if the
    ConnectionInfo is missing or its password is blank (redacted after a sync).
    """
    from utilities.models import ConnectionInfo

    conn = ConnectionInfo.objects.filter(name=CONNECTION_INFO_NAME).first()
    if conn is None:
        raise CertsrvConfigError(
            "No ConnectionInfo named '%s'. Create one (protocol=https, ip=<CA FQDN>, "
            "port=443, username=<service account>, password=<secret>) — see "
            "docs/windows-ca-cert-request-setup.md §4.1." % CONNECTION_INFO_NAME
        )
    password = getattr(conn, "password", "") or ""
    if not password:
        raise CertsrvConfigError(
            "ConnectionInfo '%s' has no password. Repo syncs redact secrets — "
            "re-enter the service-account password in the CloudBolt UI." % CONNECTION_INFO_NAME
        )
    protocol = (getattr(conn, "protocol", None) or "https").lower()
    host = getattr(conn, "ip", "") or ""
    port = int(getattr(conn, "port", None) or 443)
    username = getattr(conn, "username", "") or ""
    if not host:
        raise CertsrvConfigError("ConnectionInfo '%s' has no host/ip set." % CONNECTION_INFO_NAME)

    base_url = "%s://%s:%s" % (protocol, host, port)
    verify = CA_CERTS_FILE or VERIFY_TLS
    return WebEnrollClient(
        base_url, username, password,
        template_default=DEFAULT_TEMPLATE, verify=verify, timeout=HTTP_TIMEOUT_S,
    )


def ensure_custom_fields():
    """Idempotently pre-create the resource custom fields this content writes."""
    from infrastructure.models import CustomField

    specs = [
        (CF_COMMON_NAME, "Common Name", "STR"),
        (CF_TEMPLATE, "Certificate Template", "STR"),
        (CF_STATUS, "Certificate Status", "STR"),
        (CF_REQUEST_ID, "CA Request ID", "STR"),
        (CF_THUMBPRINT, "Certificate Thumbprint", "STR"),
        (CF_ISSUED_CERT, "Issued Certificate (PEM)", "ETXT"),
        (CF_PRIVATE_KEY, "Private Key (PEM)", "ETXT"),
    ]
    for name, label, ftype in specs:
        CustomField.objects.get_or_create(
            name=name,
            defaults={"label": label, "type": ftype, "show_on_servers": False},
        )
