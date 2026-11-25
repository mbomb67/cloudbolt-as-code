"""
POC Onboarding: Tag Mapping (MCP Tool Action plugin).

Lists, adds or removes a resource handler's tag mappings: which CloudBolt
attribute (group, owner, os_family, environment, hostname, os_build, or
any parameter shown on servers) is written to which cloud tag. The same
mappings on two handlers give the test plan's cross-cloud tagging (1.2.2)
and the cost-center tag (2.5.1). Synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
BUILTIN_ATTRIBUTES = ("environment", "group", "hostname", "os_build", "os_family", "owner")
ACTIONS = ("add", "list", "remove")


def _text(value):
    return html.unescape(value or "").strip()


def _lines(value):
    return [line.strip() for line in re.split(r"[\r\n]+", _text(value)) if line.strip()]


def _failure(message):
    return {"status": "FAILURE", "output_message": "", "error_message": message, "outputs": {"error": message}}


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


def _read_inputs():
    raw = {
        "onb_handler": """{{ onb_handler }}""",
        "onb_tag_mappings": """{{ onb_tag_mappings }}""",
        "onb_tag_action": """{{ onb_tag_action }}""",
    }
    mappings = []
    for line in _lines(raw["onb_tag_mappings"]):
        attribute, sep, label = line.partition("=")
        mappings.append((attribute.strip(), label.strip() if sep else attribute.strip()))
    return {
        "handler": _text(raw["onb_handler"]),
        "mappings": mappings,
        "action": (_text(raw["onb_tag_action"]) or "add").lower(),
    }


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
        return None, "No resource handler matches '{}'.".format(ref)
    return handler, None


def _valid_attribute(name):
    if name in BUILTIN_ATTRIBUTES:
        return True, None
    from infrastructure.models import CustomField

    cf = CustomField.objects.filter(name=name).first()
    if cf is None:
        return False, "no parameter named '{}' exists; create it with onb_parameter first".format(name)
    if not cf.show_on_servers:
        return False, "parameter '{}' is not shown on servers; set onb_param_show_on_servers=true with onb_parameter".format(name)
    return True, None


def _rows(handler, base):
    from tags.models import TaggableAttribute

    rows = []
    for ta in TaggableAttribute.objects.filter(resource_handler=handler).order_by("attribute"):
        rows.append({"id": ta.global_id, "attribute": ta.attribute, "tagName": ta.label, "bidirectionalSync": bool(ta.bidirectional_sync)})
    return rows


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may manage tag mappings.")

    inputs = _read_inputs()
    if not inputs["handler"]:
        return _failure("onb_handler is required: the handler's RH- global id or its name.")
    if inputs["action"] not in ACTIONS:
        return _failure("onb_tag_action must be one of: {}.".format(", ".join(ACTIONS)))
    if inputs["action"] != "list" and not inputs["mappings"]:
        return _failure("onb_tag_mappings is required: one attribute=Tag Name per line, for example group=Group or cost_center=CostCenter.")
    handler, error = _resolve_handler(inputs["handler"])
    if error:
        return _failure(error)

    from tags.models import TaggableAttribute

    base = _portal_url()
    results = []
    for attribute, label in inputs["mappings"] if inputs["action"] != "list" else []:
        if inputs["action"] == "add":
            ok, why = _valid_attribute(attribute)
            if not ok:
                results.append({"attribute": attribute, "status": "failed", "error": why})
                continue
            ta, created = TaggableAttribute.objects.get_or_create(resource_handler=handler, attribute=attribute, defaults={"label": label})
            changed = created
            if not created and ta.label != label:
                ta.label = label
                changed = True
            if created:
                try:
                    ta.bidirectional_sync = ta.get_default_bidirectional_sync()
                except Exception:
                    pass
            if changed:
                ta.save()
            results.append({"attribute": attribute, "tagName": ta.label, "status": "created" if created else ("updated" if changed else "already present"), "id": ta.global_id})
        else:
            deleted, _ = TaggableAttribute.objects.filter(resource_handler=handler, attribute=attribute).delete()
            results.append({"attribute": attribute, "status": "removed" if deleted else "not present"})

    outputs = {
        "handler": {"id": handler.global_id, "name": handler.name, "url": base + "/admin/resourcehandlers/{}/".format(handler.id)},
        "action": inputs["action"],
        "results": results,
        "mappings": _rows(handler, base),
        "attributes": {"builtIn": list(BUILTIN_ATTRIBUTES), "note": "Any parameter shown on servers can also be mapped."},
        "next": (
            "Repeat on the second handler with the same lines for cross-cloud consistency, then order a server and check "
            "the tags in the cloud console. Tags are written at provisioning."
        ),
    }
    message = "{}: {} mapping(s) after {}.".format(handler.name, len(outputs["mappings"]), inputs["action"])
    return _success(message, outputs)
