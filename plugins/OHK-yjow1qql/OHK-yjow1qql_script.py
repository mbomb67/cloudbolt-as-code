"""
POC Onboarding: CIT Test from Order (MCP Tool Action plugin).

Turns a finished order into a Continuous Infrastructure Testing test, the
same as the order page's "Add to CIT" action: CloudBolt replays the
order's payload on every test run and compares the outcome with the
original status (test plan 4.2.4). Synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import json
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")


def _text(value):
    return html.unescape(value or "").strip()


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
        "onb_cit_order": """{{ onb_cit_order }}""",
        "onb_cit_name": """{{ onb_cit_name }}""",
    }
    return {"order": _text(raw["onb_cit_order"]), "name": _text(raw["onb_cit_name"])}


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may create CIT tests.")

    inputs = _read_inputs()
    if not inputs["order"]:
        return _failure("onb_cit_order is required: the ORD- global id of a finished blueprint order.")
    from orders.models import Order

    order = Order.objects.filter(global_id=inputs["order"]).first() if GLOBAL_ID_RE.match(inputs["order"]) else None
    if order is None and inputs["order"].isdigit():
        order = Order.objects.filter(id=int(inputs["order"])).first()
    if order is None:
        return _failure("Order '{}' not found.".format(inputs["order"]))

    from cscv.models import ActionCITTest, CITConf, CITTest, can_order_be_tested

    base = _portal_url()
    existing = CITTest.objects.filter(order=order).first()
    if existing is not None:
        return _success(
            "Order {} is already CIT test '{}'.".format(order.global_id, existing.name),
            {"test": {"id": existing.global_id, "name": existing.name, "enabled": bool(existing.enabled), "url": base + "/cit/{}/".format(existing.id)},
             "order": {"id": order.global_id, "status": order.status}, "created": False,
             "next": "Run it with run_cit_tests (testing toolkit) or from the CIT page, then poll the job."},
        )
    if not can_order_be_tested(order):
        return _failure(
            "Order {} cannot become a CIT test: it must be finished (SUCCESS, FAILURE or WARNING) and be a blueprint "
            "order; its status is {}.".format(order.global_id, order.status)
        )
    blueprint = getattr(order, "blueprint", None)
    if blueprint is None:
        return _failure("Order {} has no blueprint.".format(order.global_id))

    try:
        from cbhooks.models import CloudBoltHook
        from django.core.files.uploadedfile import SimpleUploadedFile
        from orders.views import ACTIONCITTEST_PAYLOAD_TEMPLATE
        from servicecatalog.services.blueprint_service import BlueprintService

        service = BlueprintService(blueprint)
        try:
            still_valid = service.order_still_valid_for_blueprint(order)
        except Exception:
            still_valid = True
        ok, payload_dict = service.sample_payload(profile, order.group, order=order if still_valid else None)
        if not ok:
            return _failure("Could not build the order payload: {}".format(
                payload_dict.get("error", "unknown error") if isinstance(payload_dict, dict) else str(payload_dict)[:300]))
        payload = json.dumps(payload_dict, indent=4, sort_keys=True)
        plugin = CloudBoltHook(name="{}_ActionCIT_{}".format(blueprint.name, order.id))
        content = ACTIONCITTEST_PAYLOAD_TEMPLATE.format(order_dict_json=payload).encode("utf8")
        plugin.module_file.save("cb_plugin.py", SimpleUploadedFile("cb_plugin.py", content), save=False)
        plugin.save()
        test = ActionCITTest.objects.create(
            name=inputs["name"] or order.name,
            order=order,
            cit_conf=CITConf.objects.first() or CITConf.get_singleton(),
            expected_status=order.status,
            hook=plugin,
            blueprint=blueprint,
        )
    except Exception as exc:
        return _failure("Creating the CIT test failed: {}: {}".format(type(exc).__name__, str(exc)[:500]))

    outputs = {
        "test": {"id": test.global_id, "name": test.name, "expectedStatus": test.expected_status, "enabled": bool(test.enabled), "url": base + "/cit/{}/".format(test.id)},
        "order": {"id": order.global_id, "name": order.name, "status": order.status, "blueprint": blueprint.name},
        "created": True,
        "citUrl": base + "/cit/",
        "next": (
            "Run it now with run_cit_tests (testing toolkit) or the Run button on the CIT page and poll the job; a pass "
            "means every system in the provisioning path still works. Each run provisions and decommissions a server."
        ),
    }
    return _success("CIT test '{}' created from order {}.".format(test.name, order.global_id), outputs)
