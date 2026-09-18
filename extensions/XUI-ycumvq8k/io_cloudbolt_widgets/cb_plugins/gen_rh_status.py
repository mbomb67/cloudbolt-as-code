import json
import time

from common.methods import set_progress
from django.conf import settings
from resourcehandlers.models import ResourceHandler
from utilities.decorators import timeout

MAX_RETRIES = 2


@timeout(30)
def _verify_connection(resource_handler):
    return 1 if not resource_handler.cast().verify_connection() else 0


def _verify_with_retry(rh):
    """
    Retry verification up to MAX_RETRIES times to avoid marking a handler as
    down due to transient network latency or cloud API throttling.
    """
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return _verify_connection(rh)
        except Exception as exc:
            last_exc = exc
            set_progress(
                f"Attempt {attempt}/{MAX_RETRIES} failed for {rh.name}: {exc}"
            )
    raise last_exc


def run(job, **kwargs):
    rh_status = {"resource_handlers": []}

    for rh in ResourceHandler.objects.all():
        set_progress(f"Verifying connection for {rh.name}")
        status = {
            "name": rh.name,
            "id": rh.id,
            "tech_name": rh.resource_technology.name,
            "tech_slug": rh.resource_technology.slug,
            "status": 0,
            "time": 0,
        }

        start_time = time.time()

        try:
            status["status"] = _verify_with_retry(rh)
        except Exception as ex:
            set_progress(f"All retries exhausted for {rh.name}: {ex}")

        end_time = time.time()
        status["time"] = round((end_time - start_time), 3)

        rh_status["resource_handlers"].append(status)

    with open(f"{settings.PROSERV_DIR}/data/rh_status.json", "w") as fd:
        json.dump(rh_status, fd, indent=True)

    return "SUCCESS", "", ""
