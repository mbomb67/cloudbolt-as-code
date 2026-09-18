"""
CloudBolt resource action plugin: Empty Bucket.

Day-2 action invoked from a provisioned "AWS S3 Bucket" resource.

Provider: aws. Consult current AWS docs at each call site
(see docs/agents/external-apis.md) before writing the API calls.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "WARNING" | "FAILURE"
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def run(job, **kwargs):
    """Entry point. CloudBolt calls this when the action runs on a resource."""
    resource = kwargs.get("resource")
    set_progress("Running Empty Bucket...")

    # TODO: implement day-2 action logic here.

    return "SUCCESS", "", ""
