"""
CloudBolt build plugin for the "Request Certificate (Windows CA)" blueprint.

Ad-hoc certificate enrollment against a Microsoft AD CS Windows CA via the
Certification Authority Web Enrollment pages, over HTTPS with Basic auth (the
shared module windows_ca does the protocol work).

Two request modes on one order form, controlled by the generate_csr toggle:

  generate_csr = True
    - generated_pk (ETXT) is pre-filled by generate_options_for_generated_pk()
      with a freshly generated private key, so the user can COPY IT before
      submitting. run() uses that submitted key to build the CSR for
      common_name (+ subject_alt_names). The key is also stored, encrypted, on
      the resource.
  generate_csr = False (default)
    - the user pastes their own CSR in customer_csr; the private key never
      touches CloudBolt.

Both branches MERGE at "submit the CSR to the CA". On issuance the PEM cert is
stored in a resource custom field; on a manager-approval template the request is
left pending and the "Retrieve Pending Certificate" day-2 action completes it.

Order-form inputs (action inputs; see OHK-67bw7wgu_metadata.json):
  generate_csr (BOOL)         : let CloudBolt generate the CSR?  default False
  common_name (STR)           : subject CN  (generate_csr=True)
  subject_alt_names (STR)     : optional comma-separated DNS SANs  (generate_csr=True)
  certificate_template (STR)  : AD CS template name  default CloudBoltWebServer
  generated_pk (ETXT)         : generated private key, copied by the user  (generate_csr=True)
  customer_csr (TXT)          : user-supplied PEM CSR  (generate_csr=False)

NOTE: conditional show/hide of these fields is not wired in metadata (this repo
has no SHOWHIDE exemplar and a malformed dependency is silently dropped on
import). run() branches correctly regardless of visibility. Finish the show/hide
by wiring it in the CloudBolt UI and re-exporting — see the setup doc.

Returns (status, output_msg, error_msg).
"""
from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.windows_ca import (
    get_ca_client,
    ensure_custom_fields,
    generate_private_key_pem,
    build_csr_pem,
    cert_metadata,
    CertsrvError,
    CertsrvConfigError,
    CertificatePendingError,
    DEFAULT_TEMPLATE,
    CF_ISSUED_CERT,
    CF_PRIVATE_KEY,
    CF_REQUEST_ID,
    CF_THUMBPRINT,
    CF_TEMPLATE,
    CF_COMMON_NAME,
    CF_STATUS,
)

logger = ThreadLogger(__name__)


def generate_options_for_generated_pk(field=None, **kwargs):
    """Pre-fill the generated_pk field with a freshly generated private key.

    Returned as ``initial_value`` (with no dropdown options) so the user can copy
    and store the key before submitting. A new key is generated on each form
    render; run() uses the exact value submitted with the order, so copy the key
    shown at submit time. Only relevant when generate_csr = True.
    """
    try:
        return {"initial_value": generate_private_key_pem(), "options": []}
    except Exception as exc:  # noqa: BLE001 - never surface key material
        logger.error("Could not pre-generate a private key: %s", exc)
        return {"initial_value": "", "options": []}


def _csr_common_name(csr_pem):
    """Best-effort CN extraction from a PEM CSR (for resource naming)."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
        attrs = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return attrs[0].value if attrs else ""
    except Exception:  # noqa: BLE001
        return ""


def run(job, *args, **kwargs):
    set_progress("Windows CA: preparing the certificate request.")
    ensure_custom_fields()

    # Cardinal rule: quote every template variable. Multi-line fields use triple
    # quotes. Never log generated_pk / customer_csr contents.
    generate_csr = "{{ generate_csr }}".strip().lower() in ("true", "yes", "1", "on")
    common_name = "{{ common_name }}".strip()
    sans_raw = "{{ subject_alt_names }}".strip()
    template = "{{ certificate_template }}".strip() or DEFAULT_TEMPLATE
    generated_pk = """{{ generated_pk }}""".strip()
    customer_csr = """{{ customer_csr }}""".strip()

    sans = [s.strip() for s in sans_raw.split(",") if s.strip()]
    private_key_to_store = None

    # ---- Build or accept the CSR; the two branches converge on csr_pem --------
    try:
        if generate_csr:
            if not common_name:
                return "FAILURE", "", "A Common Name is required when CloudBolt generates the CSR."
            if not generated_pk:
                return "FAILURE", "", (
                    "The generated private key field was empty. Re-open the order form so a key "
                    "is generated, copy it for your records, then submit."
                )
            csr_pem = build_csr_pem(generated_pk, common_name, sans)
            private_key_to_store = generated_pk
        else:
            if not customer_csr:
                return "FAILURE", "", "Paste a PEM certificate signing request, or enable 'Generate the CSR for me'."
            csr_pem = customer_csr
            common_name = common_name or _csr_common_name(csr_pem)
    except (CertsrvConfigError, CertsrvError) as exc:
        logger.error("CSR preparation failed: %s", exc)
        return "FAILURE", "", str(exc)

    # ---- Submit to the CA -----------------------------------------------------
    resource = job.resource_set.first()
    set_progress("Windows CA: submitting the request for template '%s'." % template)
    try:
        client = get_ca_client()
        result = client.submit_csr(csr_pem, template)
    except CertificatePendingError as pending:
        _persist(resource, status="pending", common_name=common_name, template=template,
                 request_id=pending.request_id, private_key=private_key_to_store)
        msg = (
            "Request submitted and is PENDING manager approval (CA Request ID %s). "
            "Run the 'Retrieve Pending Certificate' action once it is approved."
            % pending.request_id
        )
        set_progress(msg)
        return "SUCCESS", msg, ""
    except (CertsrvConfigError, CertsrvError) as exc:
        logger.error("Certificate request failed: %s", exc)
        return "FAILURE", "", str(exc)

    # ---- Issued ---------------------------------------------------------------
    cert_pem = result["certificate"]
    meta = cert_metadata(cert_pem)
    _persist(
        resource, status="issued", common_name=common_name or meta["subject"], template=template,
        request_id=result.get("request_id"), private_key=private_key_to_store,
        certificate=cert_pem, thumbprint=meta["thumbprint"],
    )
    msg = "Certificate issued for %s (thumbprint %s, expires %s)." % (
        common_name or meta["subject"], meta["thumbprint"], meta["not_after"],
    )
    logger.info("Windows CA build: %s", msg)
    set_progress(msg)
    return "SUCCESS", msg, ""


def _persist(resource, status, common_name, template, request_id=None,
             private_key=None, certificate=None, thumbprint=None):
    """Write outcome to resource custom fields. Never logs key/cert material."""
    if not resource:
        return
    if common_name:
        resource.name = common_name
    resource.set_value_for_custom_field(CF_STATUS, status)
    resource.set_value_for_custom_field(CF_TEMPLATE, template)
    if common_name:
        resource.set_value_for_custom_field(CF_COMMON_NAME, common_name)
    if request_id is not None:
        resource.set_value_for_custom_field(CF_REQUEST_ID, str(request_id))
    if private_key:
        resource.set_value_for_custom_field(CF_PRIVATE_KEY, private_key)
    if certificate:
        resource.set_value_for_custom_field(CF_ISSUED_CERT, certificate)
    if thumbprint:
        resource.set_value_for_custom_field(CF_THUMBPRINT, thumbprint)
    resource.save()
