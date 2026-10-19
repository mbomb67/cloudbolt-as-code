"""
POC Onboarding: Set Item Status (MCP Tool Action plugin).

Records the state of one POC test plan item or prerequisite: done, skipped,
not_applicable or pending, with a note saying why. A customer who cannot use
SMTP, LDAP, SSO, a branded portal, Terraform or Ansible in the POC records
the item as not applicable, and the scorecard reports it that way instead
of as a failure. The record lives in a namespaced CustomField on the
appliance (the same pattern the Content Package Manager uses), so it
survives client reconnects and is visible to onb_status. Synchronous.
Admin only.

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
STATUSES = ("pending", "done", "skipped", "not_applicable")
ITEM_RE = re.compile(r"^(P\d{1,2}|\d\.\d\.\d)$")

# Test plan items and prerequisites, with the weight the scorecard applies.
KNOWN_ITEMS = {
    "P1": ("CMP license key received and installed", None),
    "P2": ("Appliance deployed, initial configuration done", None),
    "P3": ("Up to two resource handlers, one or two environments each", None),
    "P4": ("Custom Server deployed once in each environment", None),
    "P5": ("Outbound 443 to content.cloudbolt.app", None),
    "P6": ("Web proxy configured, if one is required", None),
    "P7": ("SMTP configured", None),
    "P8": ("DNS A record for the CloudBolt portal", None),
    "P9": ("SSO application and SSO SME available", None),
    "P10": ("AD/LDAP service account and group DN", None),
    "P11": ("Ansible or Ansible Automation Platform prerequisites", None),
    "P12": ("Terraform scripts in a git repository", None),
    "1.1.1": ("Provision a standardized blueprint from the self-service catalog", 5),
    "1.1.2": ("User-defined parameters during provisioning", 5),
    "1.1.3": ("Post-provisioning automation from a catalog blueprint", 5),
    "1.1.4": ("Hostname naming convention with preview", 5),
    "1.2.1": ("One blueprint deploys to two clouds", 5),
    "1.2.2": ("Cross-cloud tagging policy", 5),
    "1.2.3": ("Unified visibility of servers across clouds", 5),
    "2.1.1": ("LDAP or Active Directory authentication", 3),
    "2.1.2": ("Group and role assignment from AD group membership", 3),
    "2.1.3": ("SAML2 single sign-on", 2),
    "2.2.1": ("Per-tenant branded portal", 3),
    "2.3.1": ("Resource visibility constrained by group role", 4),
    "2.3.2": ("Blueprint entitlement by group", 4),
    "2.3.3": ("Environment entitlement by group", 4),
    "2.3.4": ("Custom role", 3),
    "2.4.1": ("Basic order approval", 3),
    "2.4.2": ("Approval on a day-2 action", 3),
    "2.5.1": ("Tagging policy from a parameter", 5),
    "2.5.2": ("Group quota", 2),
    "3.1.1": ("Power schedules", 5),
    "3.1.2": ("Leases (expiration dates)", 5),
    "3.1.3": ("Self-service decommission", 4),
    "3.1.4": ("Onboard existing cloud resources", 5),
    "3.2.1": ("Resize a VM", 4),
    "3.2.2": ("Configuration update through Ansible", 0),
    "3.2.3": ("Lifecycle actions (start, stop, reboot)", 3),
    "4.1.1": ("Agentless", 5),
    "4.1.2": ("Agentless in-guest script execution", 5),
    "4.2.1": ("UI extension from the Content Library", 5),
    "4.2.2": ("Recurring job on a custom schedule", 4),
    "4.2.3": ("Rule-based automation", 3),
    "4.2.4": ("Continuous infrastructure testing", 3),
    "4.2.5": ("Inbound webhook", 4),
    "4.2.6": ("Customizable notification templates", 0),
    "4.3.1": ("Python generated options for a parameter", 5),
    "4.3.2": ("PowerShell in-guest script as a build item", 4),
    "4.3.3": ("Bash in-guest script as a build item", 4),
    "4.3.4": ("Terraform-backed blueprint", 0),
    "4.3.5": ("Ansible Automation Platform configuration", 0),
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


def _read_inputs():
    raw = {
        "onb_item_id": """{{ onb_item_id }}""",
        "onb_item_status": """{{ onb_item_status }}""",
        "onb_item_note": """{{ onb_item_note }}""",
    }
    return {
        "item": _text(raw["onb_item_id"]).upper(),
        "status": _text(raw["onb_item_status"]).lower().replace("-", "_").replace(" ", "_"),
        "note": _text(raw["onb_item_note"])[:1000],
    }


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


def _summary(items):
    counts = {status: 0 for status in STATUSES}
    for entry in items.values():
        status = (entry or {}).get("status")
        if status in counts:
            counts[status] += 1
    return counts


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may record POC item statuses.")

    inputs = _read_inputs()
    item = inputs["item"]
    if not item:
        return _failure("onb_item_id is required: a test plan item such as 2.1.3 or a prerequisite such as P7.")
    if not ITEM_RE.match(item):
        return _failure("'{}' is not a test plan item id (1.1.1 to 4.3.5) or a prerequisite (P1 to P12).".format(item))
    status = inputs["status"] or "done"
    if status not in STATUSES:
        return _failure("onb_item_status must be one of: {}.".format(", ".join(STATUSES)))
    if status in ("skipped", "not_applicable") and not inputs["note"]:
        return _failure("Give onb_item_note a short reason when an item is skipped or not applicable; the scorecard prints it.")

    title, weight = KNOWN_ITEMS.get(item, (None, None))
    when = datetime.now(timezone.utc).isoformat()
    username = getattr(getattr(profile, "user", None), "username", None)

    def mutate(state):
        items = state.setdefault("items", {})
        items[item] = {
            "status": status,
            "note": inputs["note"],
            "title": title,
            "weight": weight,
            "updatedAt": when,
            "updatedBy": username,
        }

    state = _update_state(mutate)
    items = state.get("items") or {}
    outputs = {
        "item": item,
        "title": title,
        "weight": weight,
        "status": status,
        "note": inputs["note"],
        "recordedAt": when,
        "items": items,
        "summary": _summary(items),
    }
    if title is None:
        outputs["note"] = "'{}' is not in the current test plan; it was recorded anyway.".format(item)
    return _success("{} recorded as {}.".format(item, status), outputs)
