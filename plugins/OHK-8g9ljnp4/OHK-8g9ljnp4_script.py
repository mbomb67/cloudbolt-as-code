"""
CloudBolt teardown plugin: Teardown AWS S3 Bucket.

Idempotent reverse of the build for the "AWS S3 Bucket" blueprint.

Teardown must be tolerant of partial / already-deleted state: if the underlying
resource is already gone, log a WARNING and return SUCCESS rather than FAILURE,
so a re-run of teardown does not block decommission.

Provider: aws. Consult current AWS docs at each call site
(see docs/agents/external-apis.md) before writing the delete calls.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "WARNING" | "FAILURE"
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def run(job, **kwargs):
    """Entry point. CloudBolt calls this to tear down the resource."""
    resource = kwargs.get("resource")
    set_progress("Starting AWS S3 Bucket teardown...")

    # TODO: implement idempotent teardown logic here.
    # If the resource is already absent: logger.warning(...) and return SUCCESS.

    return "SUCCESS", "", ""
