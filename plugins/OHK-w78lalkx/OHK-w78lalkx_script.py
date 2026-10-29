"""
POC Echo (inbound webhook plugin, webhooks/IWH-f16qxnhm).

Returns whatever JSON or form fields were posted, with the calling user and
the time, so an external system can prove a synchronous, authenticated call
into CloudBolt (test plan 4.2.5).

URL (trailing slash required):
  POST /api/v3/cmp/inboundWebHooks/poc-echo/run/   with a JSON body
  GET  /api/v3/cmp/inboundWebHooks/poc-echo/run/?hello=world

Authenticate with the CloudBolt API token or session; unauthenticated calls
are rejected by CloudBolt before this code runs.
"""
from datetime import datetime, timezone


def _response(parameters, profile, method):
    user = None
    if profile is not None:
        user = getattr(getattr(profile, "user", None), "username", None)
    return {
        "received": parameters if isinstance(parameters, dict) else {"body": str(parameters)},
        "fieldCount": len(parameters) if isinstance(parameters, dict) else 0,
        "method": method,
        "caller": user,
        "receivedAt": datetime.now(timezone.utc).isoformat(),
        "webhook": "poc-echo",
    }


def inbound_web_hook_post(*args, parameters=None, profile=None, **kwargs):
    return _response(parameters or {}, profile, "POST")


def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs):
    return _response(parameters or {}, profile, "GET")
