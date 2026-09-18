# AWS S3 Bucket

Scaffold for a blueprint that provisions, discovers, empties, and tears down AWS S3 buckets as CloudBolt resources of type `s3_bucket`. The wiring (build, teardown, discovery, one day-2 action) is complete; the plugin bodies are placeholders that return SUCCESS without calling AWS. Implement them before publishing this blueprint to a catalog.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-ez6r3rqe | AWS S3 Bucket |
| Teardown | OHK-8g9ljnp4 | Teardown AWS S3 Bucket |
| Discovery | OHK-j8umy9e2 | Discover S3 Buckets |
| Day-2 action | RSA-fk574ax9 | Empty Bucket (hook OHK-pimbei86) |

## Prerequisites
- CloudBolt 8.6 or later (`minimum_version_required`).
- At least one AWS resource handler whose credentials allow the S3 calls you implement (create, list, delete objects, delete bucket).

## Setup
1. Implement `run()` in `plugins/OHK-ez6r3rqe/OHK-ez6r3rqe_script.py` and declare its order-form fields (for example bucket name and region) in the plugin's `action_inputs`.
2. Implement `run()` in `plugins/OHK-8g9ljnp4/OHK-8g9ljnp4_script.py`. Keep it idempotent: return a WARNING, not FAILURE, when the bucket is already gone.
3. Implement `discover_resources()` in `plugins/OHK-j8umy9e2/OHK-j8umy9e2_script.py` to enumerate every AWS handler and region, returning one dict per bucket with a stable unique key so re-runs update rather than duplicate.
4. Implement `run()` in `plugins/OHK-pimbei86/OHK-pimbei86_script.py` to delete the bucket's objects.
5. Sync the repo.

## Notes
- As shipped, ordering the blueprint creates an empty `S3 Bucket` resource and no AWS bucket; deleting it succeeds without touching AWS; discovery returns nothing.
- Empty Bucket is a destructive action but is not marked `dangerous` and does not require approval. Consider setting both on `RSA-fk574ax9` once implemented.
