"""
CloudBolt build plugin: AWS S3 Bucket.

Provisions the resource for the "AWS S3 Bucket" blueprint.

Expected Action Inputs (define on the plugin's action_inputs[] in metadata):
  - TODO: enumerate the order-form fields this build needs (e.g. bucket_name, region).

Provider: aws. Before writing any boto3 / AWS REST call, consult the current
official AWS docs at the call site (see docs/agents/external-apis.md) — do not
extrapolate API shapes from memory.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "WARNING" | "FAILURE"
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def run(job, **kwargs):
    """Entry point. CloudBolt calls this to build the resource."""
    resource = kwargs.get("resource")
    set_progress("Starting AWS S3 Bucket build...")

    # TODO: implement build logic here.

    return "SUCCESS", "", ""
