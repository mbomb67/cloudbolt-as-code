"""
POC Onboarding: Import OS Build (MCP Tool Action plugin).

Imports one image (an AWS AMI, an Azure marketplace image, a vCenter
template) into an OS build and attaches that OS build to the handler's
environments, the way the handler's images page does. An OS build name
that already exists is reused, so one build ("Ubuntu 22.04") can carry an
image per handler and a single blueprint deploys to both clouds. Runs as a
job because it calls the cloud API. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
LOCATION_FIELD = {"aws": "aws_region", "aws_govcloud": "aws_region", "aws_china": "aws_region", "azure_arm": "node_location"}


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
    """What ResourceHandlerBaseService.create_osbuild_and_attributes does, without the abstract service."""
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


def _import_aws(handler, image, region, os_build_name, existing_osb, job):
    from resourcehandlers.aws.models import AmazonMachineImage

    _progress(job, "Looking up image {} in {}".format(image, region))
    if image.lower().startswith("ami-"):
        discovered, not_in_cb, _only = handler.discover_templates(region, ami_ids=[image])
    else:
        discovered = handler.get_ami_dicts_for_region_by_names([image], region)
        not_in_cb = discovered
    discovered = list(discovered or [])
    if not discovered:
        return None, "AWS returned no image for '{}' in {}. Pass the AMI id (ami-...) or the exact AMI name.".format(image, region)
    image_dict = dict(discovered[0])
    ami_id = image_dict.get("ami_id") or image
    already = AmazonMachineImage.objects.filter(resourcehandler=handler, ami_id=ami_id, region=region).order_by("-id").first()
    if already is not None and already.os_build is not None:
        return already.os_build, None
    image_dict.setdefault("region", region)
    _add_template(handler, image_dict, os_build_name, existing_osb)
    osba = AmazonMachineImage.objects.filter(resourcehandler=handler, ami_id=ami_id).order_by("-id").first()
    if osba is None or osba.os_build is None:
        return None, "The image was imported but no OS build attribute for {} was found afterwards.".format(ami_id)
    return osba.os_build, None


def _azure_match(d, image):
    wanted = image.lower()
    urn = ":".join(str(d.get(k) or "") for k in ("publisher", "offer", "sku", "version")).lower()
    for key in ("template_name", "name", "azure_image_name", "label", "image_id"):
        if str(d.get(key) or "").lower() == wanted:
            return True
    return urn == wanted or urn.rsplit(":", 1)[0] == wanted


def _import_azure(handler, image, location, os_build_name, existing_osb, job):
    from c2_wrapper import guess_os_family
    from resourcehandlers.azure_arm.models import AzureARMImage

    _progress(job, "Looking up image {} in {}".format(image, location))
    available, unimported, _only = handler.discover_templates(locations=[location])
    available = list(available or [])
    if not available:
        return None, (
            "No Azure images are cached for location {}. Run Sync Images on the handler page (or wait for the "
            "images sync job), then retry.".format(location)
        )
    match = next((d for d in available if _azure_match(d, image)), None)
    if match is None:
        sample = ", ".join(str(d.get("template_name") or d.get("name")) for d in available[:15])
        return None, "No image '{}' in {}. Pass the template name or publisher:offer:sku[:version]. Some available: {}.".format(
            image, location, sample
        )
    template_name = match.get("template_name") or match.get("name")
    already = AzureARMImage.objects.filter(resourcehandler=handler, template_name=template_name, region=location).order_by("-id").first()
    if already is not None and already.os_build is not None:
        return already.os_build, None
    d = dict(match)
    d.pop("template_name", None)
    d.pop("name", None)
    d.pop("_state", None)
    family = guess_os_family(dict(match))
    osba = handler.add_template(
        template_name,
        os_build_name=os_build_name,
        os_family=family,
        os_build=existing_osb,
        region=location,
        **d,
    )
    if osba is None or getattr(osba, "os_build", None) is None:
        osba = AzureARMImage.objects.filter(resourcehandler=handler, template_name=template_name, region=location).order_by("-id").first()
    if osba is None or osba.os_build is None:
        return None, "The image was imported but no OS build attribute for {} was found afterwards.".format(template_name)
    return osba.os_build, None


def _import_vmware(handler, image, os_build_name, existing_osb, job):
    from resourcehandlers.vmware.models import VsphereOSBuildAttribute

    _progress(job, "Looking up template {}".format(image))
    discovered, not_in_cb, _only = handler.discover_templates()
    discovered = list(discovered or [])
    if not discovered:
        return None, (
            "No vCenter templates are cached yet. Run the recurring job 'Fetch and Cache available VMWare template' "
            "(Admin > Recurring Jobs) and retry."
        )
    match = next((d for d in discovered if str(d.get("name") or "").lower() == image.lower()), None)
    if match is None:
        sample = ", ".join(str(d.get("name")) for d in discovered[:20])
        return None, "No template named '{}'. Some available: {}.".format(image, sample)
    already = VsphereOSBuildAttribute.objects.filter(resourcehandler=handler, template_name=match["name"]).order_by("-id").first()
    if already is not None and already.os_build is not None:
        return already.os_build, None
    _add_template(handler, match, os_build_name, existing_osb)
    osba = VsphereOSBuildAttribute.objects.filter(resourcehandler=handler, template_name=match["name"]).order_by("-id").first()
    if osba is None or osba.os_build is None:
        return None, "The template was imported but no OS build attribute for {} was found afterwards.".format(match["name"])
    return osba.os_build, None


def _import_generic(handler, image, os_build_name, existing_osb, job):
    _progress(job, "Looking up image {}".format(image))
    discovered, not_in_cb, _only = handler.discover_templates()
    discovered = list(discovered or [])
    match = next(
        (d for d in discovered if isinstance(d, dict) and str(d.get("name") or d.get("value") or "").lower() == image.lower()),
        None,
    )
    if match is None:
        sample = ", ".join(str(d.get("name")) for d in discovered[:20] if isinstance(d, dict))
        return None, "No image named '{}'. Some available: {}.".format(image, sample or "none discovered")
    _add_template(handler, match, os_build_name, existing_osb)
    osba = handler.os_build_attributes.filter(template_name=match.get("name")).order_by("-id").first()
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
    for key in ("handler", "image", "os_build_name"):
        if not inputs[key]:
            return _failure("onb_{} is required.".format(key))
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
    existing_osb = _existing_os_build(inputs["os_build_name"])

    try:
        if slug.startswith("aws"):
            osb, error = _import_aws(handler, inputs["image"], location, inputs["os_build_name"], existing_osb, job)
        elif slug == "azure_arm":
            osb, error = _import_azure(handler, inputs["image"], location, inputs["os_build_name"], existing_osb, job)
        elif slug == "vmware":
            osb, error = _import_vmware(handler, inputs["image"], inputs["os_build_name"], existing_osb, job)
        else:
            osb, error = _import_generic(handler, inputs["image"], inputs["os_build_name"], existing_osb, job)
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
            "osFamily": osb.os_family.name if osb.os_family else None,
            "reused": existing_osb is not None and existing_osb.id == osb.id,
            "url": base + "/externalcontent/osbuilds/{}/".format(osb.id),
        },
        "attachedToEnvironments": attached,
        "next": (
            "For a second cloud, call this tool again with the same onb_os_build_name and that handler's image so one "
            "blueprint deploys to both. Then onb_trim_environment on each environment."
        ),
    }
    if osb.os_family is None:
        outputs["note"] = "The OS family could not be determined; set it on the OS build page so Windows and Linux scripts target the right servers."
    return _success("OS build '{}' holds {} and is attached to {} environment(s).".format(osb.name, inputs["image"], len(attached)), outputs)
