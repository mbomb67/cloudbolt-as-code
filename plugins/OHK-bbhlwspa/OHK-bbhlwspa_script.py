"""
POC Onboarding: Sync Images (MCP Tool Action plugin).

Fills a resource handler's image cache now, without the image whitelist,
and turns on the recurring job and rule that keep it fresh, with their
use_whitelist input mapped to false so the scheduled runs stay complete. CloudBolt's
image pickers (and onb_import_os_build) read cached images: AMIs
(AwsCachedAMI), Azure marketplace images (AzureARMAvailableImage), vCenter
templates (VMWareAvailableImage). On a new appliance the caches are empty
until the "Fetch and Cache available ..." jobs run, and with the default
whitelist they skip the images many customers want. Run it right after a
handler is verified; it is a long job (minutes to tens of minutes on AWS),
so continue with environments while it runs. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re
from datetime import datetime, timezone

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
LOCATION_FIELD = {"aws": "aws_region", "aws_govcloud": "aws_region", "aws_china": "aws_region", "azure_arm": "node_location"}
# Per technology family: the recurring job and the rule (trigger point) that refresh the cache.
CACHE_JOBS = {
    "aws": {"recurring_jobs": ["Fetch and Cache available AWS AMIs"], "rules": ["cache_available_aws_amis"]},
    "azure_arm": {"recurring_jobs": [], "rules": ["fetch_and_cache_available_azure_images"]},
    "vmware": {"recurring_jobs": ["Fetch and Cache available VMWare template"], "rules": ["fetch_and_cache_available_vmware_templates"]},
    "gcp": {"recurring_jobs": [], "rules": ["cache_available_gcp_images"]},
}
RULES_RUNNER = "Execute all Rules"


def _text(value):
    return html.unescape(value or "").strip()


def _lines(value):
    return [line.strip() for line in re.split(r"[\r\n,]+", _text(value)) if line.strip()]


def _bool(value, default):
    text = _text(value).lower()
    if not text:
        return default
    return text in ("true", "1", "yes", "y", "on")


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


def _progress(job, message):
    if job is None:
        return
    try:
        from common.methods import set_progress

        set_progress(message)
    except Exception:
        pass


def _read_inputs():
    raw = {
        "onb_handler": """{{ onb_handler }}""",
        "onb_locations": """{{ onb_locations }}""",
        "onb_enable_jobs": """{{ onb_enable_jobs }}""",
    }
    return {
        "handler": _text(raw["onb_handler"]),
        "locations": _lines(raw["onb_locations"]),
        "enable_jobs": _bool(raw["onb_enable_jobs"], True),
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
    try:
        handler = handler.cast()
    except Exception:
        pass
    return handler, None


def _env_locations(handler, slug):
    field = LOCATION_FIELD.get(slug)
    if not field:
        return []
    found = []
    for env in handler.environment_set.all():
        try:
            value = env.get_cfv(field)
        except Exception:
            value = None
        if value and str(value) not in found:
            found.append(str(value))
    return found


def _family(slug):
    return "aws" if slug.startswith("aws") else slug


def _cache_count(handler, family, locations):
    try:
        if family == "aws":
            from resourcehandlers.aws.models import AwsCachedAMI

            qs = AwsCachedAMI.objects.for_resource_handler(resource_handler=handler)
            if locations:
                qs = qs.filter(ami_regions__name__in=locations)
            return qs.distinct().count()
        if family == "azure_arm":
            from resourcehandlers.azure_arm.models import AzureARMAvailableImage

            qs = AzureARMAvailableImage.objects.for_resource_handler(handler)
            if locations:
                qs = qs.filter(locations__name__in=locations)
            return qs.distinct().count()
        if family == "vmware":
            from resourcehandlers.vmware.models import VMWareAvailableImage

            return VMWareAvailableImage.objects.filter(resource_handler=handler).count()
    except Exception:
        return None
    return None


def _refresh(handler, family, locations, job):
    if family == "aws":
        regions = locations or list(handler.current_regions() or [])
        if not regions:
            return {"refreshed": False, "note": "No region to refresh: import an environment first or pass onb_locations."}
        _progress(job, "Fetching all AMIs for {} without the whitelist (this can take a while)".format(", ".join(regions)))
        handler.get_all_templates(regions=regions, force_refresh=True, use_whitelist=False)
        return {"refreshed": True, "scope": regions}
    if family == "azure_arm":
        where = locations or _env_locations(handler, "azure_arm")
        if not where:
            return {"refreshed": False, "note": "No location to refresh: import an environment first or pass onb_locations."}
        _progress(job, "Fetching Azure images for {} without the whitelist".format(", ".join(where)))
        handler.update_cached_images(where, use_whitelist=False)
        return {"refreshed": True, "scope": where}
    if family == "vmware":
        _progress(job, "Fetching vCenter templates without the whitelist")
        handler.update_cached_images(use_whitelist=False)
        return {"refreshed": True, "scope": ["all clusters"]}
    if hasattr(handler, "update_cached_images"):
        _progress(job, "Fetching images for {}".format(handler.name))
        try:
            handler.update_cached_images(use_whitelist=False)
        except TypeError:
            handler.update_cached_images()
        return {"refreshed": True, "scope": ["all"]}
    return {"refreshed": False, "note": "This technology keeps no image cache; its images are listed live on the handler page."}


def _whitelist_off(owner, hook):
    """Map the action's use_whitelist input to false on this trigger (rule action or recurring job).

    The stock Fetch and Cache rules ship with use_whitelist mapped to true, so enabling them as they
    are would keep the nightly refresh on the whitelist. The mapping is a RunHookInputMapping whose
    default_value is a CustomFieldValue of the hook's input (named use_whitelist_a<hook id>).
    """
    from cbhooks.models import RunHookInputMapping
    from orders.models import CustomFieldValue

    hook_input = hook.input_fields.filter(name__startswith="use_whitelist").first()
    if hook_input is None:
        return "no use_whitelist input"
    mapping = owner.input_mappings.filter(hook_input=hook_input).first()
    if mapping is not None and mapping.default_value is not None and mapping.default_value.value is False:
        return "use_whitelist already false"
    cfv, _created = CustomFieldValue.objects.get_or_create(field=hook_input, boolean_value=False)
    if mapping is None:
        mapping = RunHookInputMapping.objects.create(hook_input=hook_input, default_value=cfv)
        owner.input_mappings.add(mapping)
    else:
        mapping.default_value = cfv
        mapping.save()
    return "use_whitelist set to false"


def _enable_jobs(family, base):
    from cbhooks.models import HookPointAction, TriggerPoint
    from jobs.models import RecurringJob

    spec = CACHE_JOBS.get(family, {"recurring_jobs": [], "rules": []})
    rows = []
    for name in spec["recurring_jobs"] + [RULES_RUNNER]:
        rj = RecurringJob.objects.filter(name=name).first()
        if rj is None:
            rows.append({"kind": "recurring job", "name": name, "status": "missing"})
            continue
        changed = False
        if not rj.enabled:
            rj.enabled = True
            rj.save()
            changed = True
        row = {"kind": "recurring job", "name": name, "schedule": rj.schedule, "status": "enabled" if changed else "already enabled",
               "url": base + "/recurring_jobs/"}
        if name != RULES_RUNNER:
            real = rj.cast()
            hook = getattr(real, "hook", None)
            if hook is not None:
                row["useWhitelist"] = _whitelist_off(real, hook)
            if family == "aws":
                row["note"] = ("CloudBolt's AWS cache action always applies the whitelist when it runs as a recurring job "
                               "(no regions are passed); the rule below does the full refresh per imported region.")
        rows.append(row)
    for name in spec["rules"]:
        rule = TriggerPoint.objects.filter(name=name).first()
        if rule is None:
            rows.append({"kind": "rule", "name": name, "status": "missing"})
            continue
        changed = False
        if not rule.enabled:
            rule.enabled = True
            rule.save()
            changed = True
        row = {"kind": "rule", "name": name, "status": "enabled" if changed else "already enabled", "url": base + "/actions/rules/"}
        results = []
        for action in HookPointAction.objects.filter(hook_point_id=rule.pk):
            results.append(_whitelist_off(action, action.hook))
        row["useWhitelist"] = results[0] if len(results) == 1 else (results or "no actions")
        rows.append(row)
    return rows


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may refresh image caches.")

    inputs = _read_inputs()
    if not inputs["handler"]:
        return _failure("onb_handler is required: the handler's RH- global id or its name.")
    handler, error = _resolve_handler(inputs["handler"])
    if error:
        return _failure(error)

    base = _portal_url()
    tech = handler.resource_technology
    slug = tech.slug if tech else ""
    family = _family(slug)
    locations = inputs["locations"] or _env_locations(handler, slug)
    before = _cache_count(handler, family, locations)
    started = datetime.now(timezone.utc)
    try:
        refresh = _refresh(handler, family, locations, job)
    except Exception as exc:
        return _failure(
            "Image refresh for '{}' failed: {}: {}. Run onb_verify_resource_handler; the credentials may lack the "
            "permission to list images (ec2:DescribeImages, Microsoft.Compute/images/read, vCenter template browse).".format(
                handler.name, type(exc).__name__, str(exc)[:500])
        )
    after = _cache_count(handler, family, locations)
    jobs = _enable_jobs(family, base) if inputs["enable_jobs"] else []
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    outputs = {
        "handler": {"id": handler.global_id, "name": handler.name, "technologySlug": slug},
        "refresh": refresh,
        "cachedImages": {"before": before, "after": after, "scope": locations or "all"},
        "secondsTaken": seconds,
        "whitelist": "not applied now, and the enabled rule and job are mapped to use_whitelist=false for their scheduled runs",
        "scheduledRefresh": jobs,
        "next": (
            "Pick the images with onb_import_os_build (leave onb_image empty to list the cache for a location). The "
            "recurring job and rule above keep the cache fresh on their schedule, without the whitelist."
        ),
    }
    message = "{}: cache {} -> {} image(s) in {}s; {} job/rule row(s).".format(handler.name, before, after, seconds, len(jobs))
    return _success(message, outputs)
