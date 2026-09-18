"""
Azure Price Sheet Refresh — recurring-job hook.

For every Azure Resource Manager handler, detect the subscription's billing
agreement type and, for EA / MCA / MPA, download the NEGOTIATED Azure Price Sheet
and cache it on disk (keyed by meterId) for the rate hook to read at order time.
CSP-customer, MOSP, and no-billing-access subscriptions are skipped — those price
via the public Retail Prices API at preview time instead.

The Price Sheet download is an asynchronous bulk operation (POST -> poll -> SAS
blob, CSV/ZIP), so it must run here on a schedule, never inline in a cost
preview. Schedule via the companion recurring job (default daily). The handler
service principal must hold a billing-scope role for negotiated pricing:
EA EnrollmentReader, or MCA/MPA Billing Profile Reader.

Entry point: run(job, logger=None, **kwargs) -> (status, output, errors).
"""
from resourcehandlers.azure_arm.models import AzureARMHandler
from utilities.logger import ThreadLogger

from shared_modules.azure_pricing import (
    detect_billing_scope,
    download_price_sheet,
    get_handler_token,
    write_price_cache,
)

logger = ThreadLogger(__name__)

# Agreement types whose negotiated Price Sheet we can read with the handler SPN.
_NEGOTIABLE = ("EnterpriseAgreement", "MicrosoftCustomerAgreement",
               "MicrosoftPartnerAgreement")


def run(job, logger=logger, **kwargs):
    handlers = AzureARMHandler.objects.all()
    refreshed, skipped, failed = [], [], []

    for base in handlers:
        rh = base.cast()
        label = f"{rh.name} (handler {rh.id})"
        try:
            token = get_handler_token(rh)
        except Exception as exc:
            logger.warning(f"[price-sheet-refresh] {label}: token error: {exc}")
            failed.append(label)
            continue

        scope = detect_billing_scope(rh, token)
        if not scope or scope.get("agreement_type") not in _NEGOTIABLE:
            reason = scope.get("agreement_type") if scope else "no billing access"
            logger.info(f"[price-sheet-refresh] {label}: skipping negotiated "
                        f"refresh ({reason}); rate hook will use retail.")
            skipped.append(label)
            continue

        logger.info(f"[price-sheet-refresh] {label}: agreement="
                    f"{scope.get('agreement_type')}, account="
                    f"{scope.get('billing_account_name')}, profile="
                    f"{scope.get('billing_profile_name')}; downloading price sheet.")
        try:
            meters = download_price_sheet(rh, scope, token)
        except Exception as exc:
            logger.warning(f"[price-sheet-refresh] {label}: download raised: {exc!r}")
            failed.append(label)
            continue

        if not meters:
            logger.warning(f"[price-sheet-refresh] {label}: no price sheet returned "
                           f"(download/poll/parse failed — see [azure_pricing] "
                           f"warnings above for the HTTP status/body).")
            failed.append(label)
            continue

        write_price_cache(rh, scope, meters, currency=scope.get("currency"))
        refreshed.append(f"{label} [{scope['agreement_type']}, {len(meters)} meters]")

    summary = (f"Refreshed {len(refreshed)} handler(s); "
               f"skipped {len(skipped)}; failed {len(failed)}.")
    logger.info(f"[price-sheet-refresh] {summary}")
    if refreshed:
        summary += " Refreshed: " + "; ".join(refreshed)
    # A failed handler shouldn't fail the whole job — the rate hook falls back to
    # retail. Report WARNING only when nothing succeeded but something failed.
    if failed and not refreshed:
        return "WARNING", summary, ""
    return "SUCCESS", summary, ""
