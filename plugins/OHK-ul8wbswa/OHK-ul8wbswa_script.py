"""
CloudBolt day-2 plugin for the "Request Certificate (Windows CA)" blueprint:
retrieve a certificate whose request was left PENDING by a manager-approval
template.

The build plugin stores the CA Request ID on the resource (windows_ca_request_id)
when the CA returns a pending disposition. This action re-polls the Web Enrollment
pages for that Request ID; once an approver has issued the certificate, it stores
the PEM cert and metadata on the resource and flips the status to issued.

Bound to the blueprint via management_actions -> RSA-7cjsqrwy -> this plugin.

Returns (status, output_msg, error_msg).
"""
from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.windows_ca import (
    get_ca_client,
    ensure_custom_fields,
    cert_metadata,
    CertsrvError,
    CertsrvConfigError,
    CF_ISSUED_CERT,
    CF_REQUEST_ID,
    CF_THUMBPRINT,
    CF_COMMON_NAME,
    CF_STATUS,
)

logger = ThreadLogger(__name__)


def run(job, *args, **kwargs):
    ensure_custom_fields()
    resource = kwargs.get("resource") or job.resource_set.first()
    if not resource:
        return "FAILURE", "", "No resource in context for this action."

    request_id = (resource.get_value_for_custom_field(CF_REQUEST_ID) or "").strip()
    if not request_id:
        return "FAILURE", "", (
            "This certificate has no pending CA Request ID — nothing to retrieve "
            "(it was issued immediately, or the request never reached the CA)."
        )

    set_progress("Windows CA: polling for pending request %s." % request_id)
    try:
        client = get_ca_client()
        result = client.retrieve_pending(int(request_id))
    except (CertsrvConfigError, CertsrvError) as exc:
        logger.error("Retrieve pending certificate failed: %s", exc)
        return "FAILURE", "", str(exc)
    except ValueError:
        return "FAILURE", "", "Stored CA Request ID '%s' is not a valid integer." % request_id

    if result["status"] != "issued":
        msg = "Request %s is still pending approval. Try again once an approver issues it." % request_id
        set_progress(msg)
        return "SUCCESS", msg, ""

    cert_pem = result["certificate"]
    meta = cert_metadata(cert_pem)
    resource.set_value_for_custom_field(CF_ISSUED_CERT, cert_pem)
    resource.set_value_for_custom_field(CF_THUMBPRINT, meta["thumbprint"])
    resource.set_value_for_custom_field(CF_STATUS, "issued")
    common_name = (resource.get_value_for_custom_field(CF_COMMON_NAME) or meta["subject"])
    resource.save()

    msg = "Certificate issued for %s (thumbprint %s, expires %s)." % (
        common_name, meta["thumbprint"], meta["not_after"],
    )
    logger.info("Windows CA retrieve: %s", msg)
    set_progress(msg)
    return "SUCCESS", msg, ""
