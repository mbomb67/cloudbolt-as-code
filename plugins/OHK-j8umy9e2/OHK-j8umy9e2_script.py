"""
CloudBolt discovery plugin: Discover S3 Buckets.

Syncs existing AWS S3 Buckets into CloudBolt as resources of the blueprint's
resource_type. Enumerate across ALL relevant Handler instances (every configured
AWS resource handler / region) — do not assume a single account or region.

Provider: aws. Consult current AWS docs at each call site
(see docs/agents/external-apis.md) before writing the list/describe calls.

discover_resources(**kwargs) returns a list of dicts; each dict's keys map to
custom fields on the resource, and must include RESOURCE_IDENTIFIER as a stable
unique key so re-runs update rather than duplicate.
"""

from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Stable unique key field used to de-duplicate discovered resources.
RESOURCE_IDENTIFIER = "bucket_name"


def discover_resources(**kwargs):
    """Entry point. Return a list of dicts, one per discovered resource."""
    discovered = []

    # TODO: enumerate all AWS handlers/regions and append one dict per bucket,
    # each containing the RESOURCE_IDENTIFIER key.

    return discovered
