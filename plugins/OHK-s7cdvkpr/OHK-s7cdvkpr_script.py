"""
POC Onboarding: Verify Resource Handler (MCP Tool Action plugin).

Calls the handler's own Verify Connection and returns the real exception
text, which the UI form replaces with "Unable to connect to handler with
the information provided." and the REST verify endpoint drops. Adds a
technology-specific hint for the error, the link to the minimum-permissions
document, and the links to the handler's edit and verify pages; records the
result so onb_status can show it. Runs as a job (asynchronous) because the
check calls the cloud API. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import json
import re
from datetime import datetime, timezone

STATE_NAMESPACE = "poc_onboarding"
STATE_FIELD = "poc_onboarding_state"
GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")

PERMISSION_DOCS = {
    "aws": "https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/aws/a/h2__2010883827",
    "aws_govcloud": "https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/aws/a/h2__2010883827",
    "aws_china": "https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/aws/a/h2__2010883827",
    "azure_arm": "https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/azure-resource-manager/a/h3_736726059",
    "vmware": "https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/minimum-required-permissions-in-vcenter-vmware",
}
DEFAULT_DOC = "https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/resource-handlers"

GENERIC_HINTS = [
    (re.compile(r"CERTIFICATE_VERIFY_FAILED|certificate verify failed|SSLError", re.I), "TLS verification failed. Add the endpoint's CA under Admin > SSL Certificates, or turn off SSL verification on the handler for the POC."),
    (re.compile(r"Name or service not known|nodename nor servname|getaddrinfo failed", re.I), "The appliance cannot resolve the endpoint hostname in DNS."),
    (re.compile(r"Connection refused|timed out|ConnectTimeout|EndpointConnectionError|Max retries exceeded|Network is unreachable", re.I), "The appliance cannot reach the endpoint on its port. Check outbound firewall rules (443 to the cloud endpoint) and the web proxy under Admin > Miscellaneous Settings."),
    (re.compile(r"ProxyError|407 Proxy", re.I), "The outbound proxy refused the request; check its host, port and credentials."),
]
TECH_HINTS = {
    "aws": [
        (re.compile(r"InvalidClientTokenId|SignatureDoesNotMatch|AuthFailure", re.I), "AWS rejected the access key id or secret access key. Re-enter both on the handler's edit page; a key copied with a trailing space fails the same way."),
        (re.compile(r"UnauthorizedOperation|AccessDenied|not authorized", re.I), "The IAM identity lacks a permission CloudBolt needs (ec2:Describe* at least). Attach the policy from the minimum permissions document."),
        (re.compile(r"ExpiredToken|RequestExpired", re.I), "The credentials or session token have expired; the appliance clock may also be wrong."),
        (re.compile(r"AssumeRole|role_arn|sts", re.I), "The assume-role step failed; check the role ARN, its trust policy and the external id."),
    ],
    "azure_arm": [
        (re.compile(r"AADSTS7000215", re.I), "The client secret is invalid or expired. Create a new secret on the app registration (Certificates & secrets) and paste the secret value, not its id."),
        (re.compile(r"AADSTS700016|AADSTS90002|was not found in the directory", re.I), "Entra cannot find the application or tenant: check the tenant id and the application (client) id."),
        (re.compile(r"AuthorizationFailed|does not have authorization|SubscriptionNotFound", re.I), "The service principal has no role on the subscription, or the subscription id is wrong. Assign Reader plus the roles in the Azure permissions document."),
        (re.compile(r"InvalidAuthenticationTokenTenant", re.I), "The token was issued by a different tenant than the one that owns the subscription."),
    ],
    "vmware": [
        (re.compile(r"Cannot complete login due to an incorrect user name or password|incorrect user name", re.I), "vCenter rejected the username or password. Use the user@domain form and re-enter the password."),
        (re.compile(r"Permission to perform this operation was denied|NoPermission", re.I), "The vCenter role lacks a privilege CloudBolt needs; compare with the vCenter minimum permissions document."),
        (re.compile(r"Connection refused|timed out", re.I), "Port 443 from the appliance to vCenter (and to every ESX host, for console and uploads) must be open."),
    ],
    "scvmm": [
        (re.compile(r"WinRM|5985|5986|Negotiate|Kerberos|NTLM", re.I), "CloudBolt talks to SCVMM over WinRM; check the port (5985 or 5986), the authentication type and that the account is an SCVMM administrator."),
    ],
    "gcp": [
        (re.compile(r"invalid_grant|Invalid JWT|private key", re.I), "The service account JSON is invalid or its key was revoked; download a fresh key file and re-upload it."),
        (re.compile(r"PERMISSION_DENIED|403", re.I), "The service account lacks a role on the project (Compute Admin at least)."),
    ],
    "acropolis": [
        (re.compile(r"401|Unauthorized|AUTHENTICATION_REQUIRED", re.I), "Prism rejected the credentials; use a Prism Element or Central account with cluster admin rights."),
    ],
    "openstack": [
        (re.compile(r"Unauthorized|401|The request you have made requires authentication", re.I), "Keystone rejected the credentials; check the domain, project and user, and that the auth URL ends in /v3."),
    ],
}


def _text(value):
    return html.unescape(value or "").strip()


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


def _success(message, outputs):
    return {"status": "SUCCESS", "output_message": message, "error_message": "", "outputs": outputs}


def _caller(job, kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    return profile


def _portal_url():
    try:
        from portals.models import PortalConfig

        portal = PortalConfig.get_current_portal()
        url = portal.site_url if portal else None
    except Exception:
        url = None
    return (url or "").rstrip("/")


def _progress(job, message):
    if job is None:
        return
    try:
        from common.methods import set_progress

        set_progress(message)
    except Exception:
        pass


def _read_inputs():
    raw = {"onb_handler": """{{ onb_handler }}"""}
    return {"handler": _text(raw["onb_handler"])}


def _state_field(create=False):
    from infrastructure.models import CustomField, Namespace

    if not create:
        return CustomField.objects.filter(name=STATE_FIELD).first()
    namespace, _ = Namespace.objects.get_or_create(name=STATE_NAMESPACE)
    field, _ = CustomField.objects.get_or_create(
        name=STATE_FIELD,
        defaults=dict(
            label="POC Onboarding State",
            description="State recorded by the POC onboarding MCP tools: item statuses, handler verify results, install details.",
            type="TXT",
            namespace=namespace,
        ),
    )
    return field


def _load_state():
    from orders.models import CustomFieldValue

    field = _state_field()
    if field is None:
        return {}
    cfv = CustomFieldValue.objects.filter(field=field).order_by("id").first()
    if cfv is None:
        return {}
    try:
        data = json.loads(cfv.txt_value or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _update_state(mutate):
    from django.db import transaction
    from infrastructure.models import CustomField
    from orders.models import CustomFieldValue

    field = _state_field(create=True)
    with transaction.atomic():
        list(CustomField.objects.select_for_update().filter(pk=field.pk))
        cfv = CustomFieldValue.objects.filter(field=field).order_by("id").first()
        if cfv is None:
            cfv = CustomFieldValue(field=field)
            state = {}
        else:
            try:
                state = json.loads(cfv.txt_value or "")
            except ValueError:
                state = {}
            if not isinstance(state, dict):
                state = {}
        mutate(state)
        cfv.value = json.dumps(state, indent=2)
        cfv.save()
    return state


def _resolve_handler(ref):
    from resourcehandlers.models import ResourceHandler

    handler = None
    if GLOBAL_ID_RE.match(ref):
        handler = ResourceHandler.objects.filter(global_id=ref).first()
    elif ref.isdigit():
        handler = ResourceHandler.objects.filter(id=int(ref)).first()
    if handler is None:
        matches = list(ResourceHandler.objects.filter(name__iexact=ref))
        if len(matches) > 1:
            return None, "Several resource handlers are named '{}'; pass the RH- global id.".format(ref)
        handler = matches[0] if matches else None
    if handler is None:
        available = ", ".join(
            "{} ({})".format(h.global_id, h.name) for h in ResourceHandler.objects.all().order_by("id")
        ) or "none"
        return None, "No resource handler matches '{}'. Available: {}.".format(ref, available)
    try:
        handler = handler.cast()
    except Exception:
        pass
    return handler, None


def _hints(slug, error_text):
    hints = []
    family = "aws" if (slug or "").startswith("aws") else slug
    for pattern, hint in TECH_HINTS.get(family, []) + GENERIC_HINTS:
        if pattern.search(error_text) and hint not in hints:
            hints.append(hint)
    if not hints:
        hints.append(
            "The error text above is the cloud API's own message. Re-check the credentials on the edit page, "
            "then the minimum permissions document, then outbound connectivity from the appliance."
        )
    return hints


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may verify resource handlers.")

    ref = _read_inputs()["handler"]
    if not ref:
        return _failure("onb_handler is required: the handler's RH- global id or its name.")
    handler, error = _resolve_handler(ref)
    if error:
        return _failure(error)

    base = _portal_url()
    tech = handler.resource_technology
    slug = tech.slug if tech else ""
    links = {
        "detail": base + "/admin/resourcehandlers/{}/".format(handler.id),
        "edit": base + "/admin/resourcehandlers/{}/edit/".format(handler.id),
        "verifyDialog": base + "/admin/resourcehandlers/{}/verify_connection/".format(handler.id),
        "importLocations": base + "/admin/resourcehandlers/{}/import_locations/".format(handler.id),
        "permissionsDoc": PERMISSION_DOCS.get(slug, DEFAULT_DOC),
    }
    outputs = {
        "handler": {
            "id": handler.global_id,
            "pk": handler.id,
            "name": handler.name,
            "technology": tech.name if tech else None,
            "technologySlug": slug or None,
            "endpoint": handler.ip,
            "environments": handler.environment_set.count(),
        },
        "links": links,
    }

    _progress(job, "Verifying connection of {} ({})".format(handler.name, tech.name if tech else "unknown technology"))
    if not getattr(handler, "can_verify_connection", True):
        outputs.update({"verified": None, "note": "This technology does not support Verify Connection."})
        return _success("{} does not support verification.".format(handler.name), outputs)

    when = datetime.now(timezone.utc).isoformat()
    try:
        handler.verify_connection()
        result = {"ok": True, "when": when, "error": None}
        outputs.update({"verified": True, "checkedAt": when})
        message = "Connection to '{}' verified.".format(handler.name)
        outputs["next"] = (
            "Start onb_sync_images for this handler now (it fills the image cache without the whitelist and enables the "
            "cache jobs; it runs for a while), then onb_handler_locations and onb_import_environments while it runs."
        )
    except Exception as exc:  # the cloud wrapper raises its own exception types
        error_text = "{}: {}".format(type(exc).__name__, str(exc))[:3000]
        result = {"ok": False, "when": when, "error": error_text}
        outputs.update(
            {
                "verified": False,
                "checkedAt": when,
                "error": error_text,
                "hints": _hints(slug, error_text),
            }
        )
        message = "Connection to '{}' failed: {}".format(handler.name, error_text[:300])
        outputs["next"] = (
            "Explain the error and the first hint to the user. Credentials and permissions are fixed on links.edit "
            "(never ask for the secret in chat); connectivity, DNS, proxy and TLS are fixed on the appliance or network "
            "side. Then run this tool again. For appliance-side context call onb_recent_errors with "
            "onb_contains='{}'.".format(handler.name)
        )

    try:
        def mutate(state):
            handlers = state.setdefault("handlers", {})
            entry = handlers.setdefault(handler.global_id, {})
            entry["name"] = handler.name
            entry["lastVerify"] = result

        _update_state(mutate)
    except Exception as exc:
        outputs["stateNote"] = "Could not record the result for onb_status: {}".format(str(exc)[:200])

    return _success(message, outputs)
