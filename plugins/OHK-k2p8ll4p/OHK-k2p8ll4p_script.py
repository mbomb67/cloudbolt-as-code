"""
POC Onboarding: Import OS Build (MCP Tool Action plugin).

Imports one image (an AWS AMI, an Azure marketplace image, a vCenter
template) into an OS build and attaches that OS build to the handler's
environments, the way the handler's images page does. With onb_image empty
it lists the cached images for the location; a partial name narrows the
list and imports when exactly one image matches. The image caches come
from onb_sync_images (or the Fetch and Cache recurring jobs); an AMI id
that is not cached is fetched on the spot, and an empty Azure or vCenter
cache is refreshed without the whitelist before the lookup. An OS build
name that already exists is reused, so one build can carry an image per
handler and a single blueprint deploys to both clouds. Runs as a job
because it calls the cloud API. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
LOCATION_FIELD = {"aws": "aws_region", "aws_govcloud": "aws_region", "aws_china": "aws_region", "azure_arm": "node_location"}
MAX_LIST = 60


def _text(value):
    return html.unescape(value or "").strip()


def _lines(value):
    return [line.strip() for line in re.split(r"[\r\n,]+", _text(value)) if line.strip()]


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
        "onb_image": """{{ onb_image }}""",
        "onb_os_build_name": """{{ onb_os_build_name }}""",
        "onb_environments": """{{ onb_environments }}""",
        "onb_region": """{{ onb_region }}""",
    }
    return {
        "handler": _text(raw["onb_handler"]),
        "image": _text(raw["onb_image"]),
        "os_build_name": _text(raw["onb_os_build_name"]),
        "environments": _lines(raw["onb_environments"]),
        "region": _text(raw["onb_region"]),
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


def _resolve_envs(handler, refs):
    envs = list(handler.environment_set.all().order_by("id"))
    if not refs:
        return envs, None
    chosen = []
    for ref in refs:
        match = None
        for env in envs:
            if env.global_id == ref or str(env.id) == ref or env.name.lower() == ref.lower():
                match = env
                break
        if match is None:
            return None, "No environment '{}' on handler {}. Its environments: {}.".format(
                ref, handler.name, ", ".join("{} ({})".format(e.global_id, e.name) for e in envs) or "none"
            )
        chosen.append(match)
    return chosen, None


def _location_of(envs, slug):
    field = LOCATION_FIELD.get(slug)
    if not field:
        return None
    for env in envs:
        try:
            value = env.get_cfv(field)
        except Exception:
            value = None
        if value:
            return str(value)
    return None


def _existing_os_build(name):
    from externalcontent.models import OSBuild

    return OSBuild.objects.filter(name__iexact=name).order_by("id").first()


def _add_template(handler, image_dict, os_build_name, existing_osb):
    """What the handler service's create_osbuild_and_attributes does, without the abstract service."""
    from c2_wrapper import guess_os_family

    image = dict(image_dict)
    if not image.get("os_family"):
        image["os_family"] = guess_os_family(image)
    image["reported_os"] = image.get("guest_os")
    image.pop("os_build_name", None)
    if existing_osb is not None:
        image.pop("os_build", None)
        return handler.add_template(os_build=existing_osb, **image)
    return handler.add_template(os_build_name=os_build_name, **image)


class Choices(Exception):
    """Raised when the lookup found several (or no) images and the agent must choose."""

    def __init__(self, rows, note):
        super().__init__(note)
        self.rows = rows
        self.note = note


# --- AWS -------------------------------------------------------------------

def _aws_rows(qs):
    rows = []
    for ami in qs[:MAX_LIST]:
        rows.append({"amiId": ami.ami_id, "name": (ami.name or "")[:120], "platform": ami.platform, "owner": ami.owner_alias or ami.owner_id,
                     "public": bool(ami.public), "architecture": ami.architecture})
    return rows


def _import_aws(handler, image, region, os_build_name, existing_osb, job):
    from resourcehandlers.aws.models import AmazonMachineImage, AwsCachedAMI

    cache = AwsCachedAMI.objects.for_resource_handler(resource_handler=handler).filter(ami_regions__name__in=[region]).distinct()
    cached_total = cache.count()
    if not image:
        raise Choices(_aws_rows(cache.order_by("name")), "{} cached AMI(s) in {}{}.".format(
            cached_total, region,
            "" if cached_total else "; the cache is empty, run onb_sync_images for this handler or pass an AMI id"))
    if image.lower().startswith("ami-"):
        ami_id = image
        if not cache.filter(ami_id=ami_id).exists():
            _progress(job, "Fetching {} from AWS into the cache".format(ami_id))
            handler.get_all_templates(image_ids=[ami_id], regions=[region], force_refresh=True, use_whitelist=False)
    else:
        matches = cache.filter(name__icontains=image).order_by("name")
        count = matches.count()
        if count != 1:
            raise Choices(_aws_rows(matches), "{} cached AMI(s) in {} match '{}'{}.".format(
                count, region, image,
                "; pass the AMI id of the one to import" if count else
                "; the cache may be empty (run onb_sync_images) or the name differs; pass the AMI id"))
        ami_id = matches.first().ami_id
    already = AmazonMachineImage.objects.filter(resourcehandler=handler, ami_id=ami_id, region=region).order_by("-id").first()
    if already is not None and already.os_build is not None:
        return already.os_build, None
    discovered, _not_in_cb, _only = handler.discover_templates(region, ami_ids=[ami_id])
    discovered = list(discovered or [])
    if not discovered:
        return None, "AWS returned no image for {} in {}; check the id and that the credentials may describe it.".format(ami_id, region)
    image_dict = dict(discovered[0])
    image_dict.setdefault("region", region)
    _add_template(handler, image_dict, os_build_name, existing_osb)
    osba = AmazonMachineImage.objects.filter(resourcehandler=handler, ami_id=ami_id).order_by("-id").first()
    if osba is None or osba.os_build is None:
        return None, "The image was imported but no OS build attribute for {} was found afterwards.".format(ami_id)
    return osba.os_build, None


# --- Azure -----------------------------------------------------------------

def _azure_urn(d):
    return ":".join(str(d.get(k) or "") for k in ("publisher", "offer", "sku", "version"))


def _azure_rows(items):
    return [{"templateName": d.get("template_name") or d.get("name"), "urn": _azure_urn(d), "os": d.get("os_type") or d.get("guest_os")} for d in items[:MAX_LIST]]


def _azure_match(d, image):
    wanted = image.lower()
    urn = _azure_urn(d).lower()
    for key in ("template_name", "name", "azure_image_name", "label", "image_id"):
        if str(d.get(key) or "").lower() == wanted:
            return "exact"
    if urn == wanted or urn.rsplit(":", 1)[0] == wanted:
        return "exact"
    if wanted in urn or any(wanted in str(d.get(k) or "").lower() for k in ("template_name", "name", "azure_image_name")):
        return "partial"
    return None


def _import_azure(handler, image, location, os_build_name, existing_osb, job):
    from resourcehandlers.azure_arm.models import AzureARMImage

    available, _unimported, _only = handler.discover_templates(locations=[location])
    available = list(available or [])
    if not available:
        _progress(job, "No cached Azure images for {}; fetching them without the whitelist".format(location))
        handler.update_cached_images([location], use_whitelist=False)
        available, _unimported, _only = handler.discover_templates(locations=[location])
        available = list(available or [])
    if not image:
        raise Choices(_azure_rows(sorted(available, key=lambda d: str(d.get("template_name") or ""))),
                      "{} cached image(s) in {}.".format(len(available), location))
    exact = [d for d in available if _azure_match(d, image) == "exact"]
    partial = [d for d in available if _azure_match(d, image) == "partial"]
    candidates = exact or partial
    if len(candidates) != 1:
        raise Choices(_azure_rows(candidates), "{} image(s) in {} match '{}'; pass the template name or publisher:offer:sku:version.".format(
            len(candidates), location, image))
    match = candidates[0]
    template_name = match.get("template_name") or match.get("name")
    already = AzureARMImage.objects.filter(resourcehandler=handler, template_name=template_name, region=location).order_by("-id").first()
    if already is not None and already.os_build is not None:
        return already.os_build, None
    from c2_wrapper import guess_os_family

    d = dict(match)
    d.pop("template_name", None)
    d.pop("name", None)
    d.pop("_state", None)
    osba = handler.add_template(template_name, os_build_name=os_build_name, os_family=guess_os_family(dict(match)),
                                os_build=existing_osb, region=location, **d)
    if osba is None or getattr(osba, "os_build", None) is None:
        osba = AzureARMImage.objects.filter(resourcehandler=handler, template_name=template_name, region=location).order_by("-id").first()
    if osba is None or osba.os_build is None:
        return None, "The image was imported but no OS build attribute for {} was found afterwards.".format(template_name)
    return osba.os_build, None


# --- vCenter ---------------------------------------------------------------

def _vmware_rows(items):
    return [{"name": d.get("name"), "guestOs": d.get("guest_os"), "type": d.get("type")} for d in items[:MAX_LIST]]


def _import_vmware(handler, image, os_build_name, existing_osb, job):
    from resourcehandlers.vmware.models import VsphereOSBuildAttribute

    discovered, _not_in_cb, _only = handler.discover_templates()
    discovered = list(discovered or [])
    if not discovered:
        _progress(job, "No cached vCenter templates; fetching them without the whitelist")
        handler.update_cached_images(use_whitelist=False)
        discovered, _not_in_cb, _only = handler.discover_templates()
        discovered = list(discovered or [])
    if not image:
        raise Choices(_vmware_rows(sorted(discovered, key=lambda d: str(d.get("name") or ""))), "{} cached template(s).".format(len(discovered)))
    exact = [d for d in discovered if str(d.get("name") or "").lower() == image.lower()]
    partial = [d for d in discovered if image.lower() in str(d.get("name") or "").lower()]
    candidates = exact or partial
    if len(candidates) != 1:
        raise Choices(_vmware_rows(candidates), "{} template(s) match '{}'; pass the exact name.".format(len(candidates), image))
    match = candidates[0]
    already = VsphereOSBuildAttribute.objects.filter(resourcehandler=handler, template_name=match["name"]).order_by("-id").first()
    if already is not None and already.os_build is not None:
        return already.os_build, None
    _add_template(handler, match, os_build_name, existing_osb)
    osba = VsphereOSBuildAttribute.objects.filter(resourcehandler=handler, template_name=match["name"]).order_by("-id").first()
    if osba is None or osba.os_build is None:
        return None, "The template was imported but no OS build attribute for {} was found afterwards.".format(match["name"])
    return osba.os_build, None


# --- other technologies ----------------------------------------------------

def _import_generic(handler, image, os_build_name, existing_osb, job):
    discovered, _not_in_cb, _only = handler.discover_templates()
    discovered = [d for d in (discovered or []) if isinstance(d, dict)]
    if not image:
        raise Choices([{"name": d.get("name") or d.get("value")} for d in discovered[:MAX_LIST]], "{} image(s) discovered.".format(len(discovered)))
    candidates = [d for d in discovered if str(d.get("name") or d.get("value") or "").lower() == image.lower()]
    if len(candidates) != 1:
        raise Choices([{"name": d.get("name") or d.get("value")} for d in discovered[:MAX_LIST]], "{} image(s) match '{}'.".format(len(candidates), image))
    _add_template(handler, candidates[0], os_build_name, existing_osb)
    osba = handler.os_build_attributes.filter(template_name=candidates[0].get("name")).order_by("-id").first()
    if osba is None or osba.os_build is None:
        return None, "The image was imported but no OS build attribute was found afterwards."
    return osba.os_build, None


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may import OS builds.")

    inputs = _read_inputs()
    if not inputs["handler"]:
        return _failure("onb_handler is required.")
    if inputs["image"] and not inputs["os_build_name"]:
        return _failure("onb_os_build_name is required when onb_image names an image to import.")
    handler, error = _resolve_handler(inputs["handler"])
    if error:
        return _failure(error)
    envs, error = _resolve_envs(handler, inputs["environments"])
    if error:
        return _failure(error)
    if not envs:
        return _failure("Handler {} has no environments yet; run onb_import_environments first.".format(handler.name))

    base = _portal_url()
    tech = handler.resource_technology
    slug = tech.slug if tech else ""
    location = inputs["region"] or _location_of(envs, slug)
    if slug in LOCATION_FIELD and not location:
        return _failure("Pass onb_region: the region or location of the image (none could be read from the environments).")
    existing_osb = _existing_os_build(inputs["os_build_name"]) if inputs["os_build_name"] else None
    from externalcontent.models import OSBuild

    osb_ids_before = set(OSBuild.objects.values_list("id", flat=True))

    try:
        if slug.startswith("aws"):
            osb, error = _import_aws(handler, inputs["image"], location, inputs["os_build_name"], existing_osb, job)
        elif slug == "azure_arm":
            osb, error = _import_azure(handler, inputs["image"], location, inputs["os_build_name"], existing_osb, job)
        elif slug == "vmware":
            osb, error = _import_vmware(handler, inputs["image"], inputs["os_build_name"], existing_osb, job)
        else:
            osb, error = _import_generic(handler, inputs["image"], inputs["os_build_name"], existing_osb, job)
    except Choices as choices:
        outputs = {
            "handler": {"id": handler.global_id, "name": handler.name, "technologySlug": slug},
            "location": location,
            "filter": inputs["image"] or None,
            "images": choices.rows,
            "note": choices.note,
            "next": "Call again with onb_image set to one AMI id, template name or publisher:offer:sku:version from the list, and onb_os_build_name.",
        }
        if inputs["image"]:
            return {"status": "FAILURE", "output_message": "", "error_message": choices.note, "outputs": outputs}
        return _success(choices.note, outputs)
    except Exception as exc:
        return _failure("Importing '{}' on {} failed: {}: {}".format(inputs["image"], handler.name, type(exc).__name__, str(exc)[:600]))
    if error:
        return _failure(error)

    attached = []
    for env in envs:
        if not osb.environments.filter(id=env.id).exists():
            osb.environments.add(env)
        attached.append({"id": env.global_id, "name": env.name})

    outputs = {
        "handler": {"id": handler.global_id, "name": handler.name, "technologySlug": slug},
        "image": inputs["image"],
        "location": location,
        "osBuild": {
            "id": osb.global_id,
            "name": osb.name,
            "requestedName": inputs["os_build_name"],
            "osFamily": osb.os_family.name if osb.os_family else None,
            "reused": osb.id in osb_ids_before,
            "url": base + "/externalcontent/osbuilds/{}/".format(osb.id),
        },
        "attachedToEnvironments": attached,
        "next": (
            "For a second cloud, call this tool again with the same onb_os_build_name and that handler's image so one "
            "blueprint deploys to both. Then onb_trim_environment on each environment."
        ),
    }
    notes = []
    if osb.os_family is None:
        notes.append("The OS family could not be determined; set it on the OS build page so Windows and Linux scripts target the right servers.")
    if osb.name.lower() != inputs["os_build_name"].lower():
        notes.append(
            "CloudBolt attached the image to its existing OS build '{}' (this image was already known under that build) "
            "instead of '{}'. For one blueprint across two clouds, import the other handler's image with "
            "onb_os_build_name='{}'.".format(osb.name, inputs["os_build_name"], osb.name)
        )
    if notes:
        outputs["notes"] = notes
    return _success("OS build '{}' holds {} and is attached to {} environment(s).".format(osb.name, inputs["image"], len(attached)), outputs)
