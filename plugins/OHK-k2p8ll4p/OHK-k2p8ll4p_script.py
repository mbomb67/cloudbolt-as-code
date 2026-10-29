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
        "onb_replace_existing": """{{ onb_replace_existing }}""",
    }
    return {
        "handler": _text(raw["onb_handler"]),
        "image": _text(raw["onb_image"]),
        "os_build_name": _text(raw["onb_os_build_name"]),
        "environments": _lines(raw["onb_environments"]),
        "region": _text(raw["onb_region"]),
        "replace_existing": _text(raw["onb_replace_existing"]).lower() in ("true", "1", "yes", "y"),
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


# Set by run() for this execution: whether the caller allowed replacing a handler's other image
# in the OS build, and what was removed.
_STATE = {"replace": False, "removed": []}


class NeedsReplace(Exception):
    """The OS build already holds another image of this handler (in this region or location)."""

    def __init__(self, osb, attrs):
        super().__init__("OS build '{}' already holds another image on this handler".format(osb.name))
        self.osb = osb
        self.attrs = attrs


def _describe_osba(attr):
    row = {"id": attr.id, "template": getattr(attr, "template_name", None) or str(attr)}
    for key in ("ami_id", "region"):
        value = getattr(attr, key, None)
        if value:
            row[key] = value
    return row


def _conflicting_osbas(handler, osb, image):
    """Images of this handler already in the OS build that the new image would collide with.

    CloudBolt resolves exactly one image per OS build and handler, per region for AWS and per
    location for Azure (osba_for_resource_handler raises "There should only be one OSBA in common
    between a resource handler and an OSB" otherwise), so a second one breaks every order.
    """
    rows = []
    try:
        attrs = list(handler.os_build_attributes.filter(os_build=osb))
    except Exception:
        return rows
    want_name = str(image.get("name") or "")
    want_ami = str(image.get("ami_id") or "")
    want_region = str(image.get("region") or "")
    for attr in attrs:
        try:
            real = attr.cast()
        except Exception:
            real = attr
        if want_ami and getattr(real, "ami_id", None) == want_ami:
            continue
        if not want_ami and want_name and (getattr(real, "template_name", "") or "") == want_name:
            continue
        region = getattr(real, "region", None)
        if want_region and region and str(region) != want_region:
            continue
        rows.append(real)
    return rows


def _add_template(handler, image_dict, os_build_name, existing_osb):
    """What the handler service's create_osbuild_and_attributes does, without the abstract service."""
    from c2_wrapper import guess_os_family

    image = dict(image_dict)
    if not image.get("os_family"):
        image["os_family"] = guess_os_family(image)
    image["reported_os"] = image.get("guest_os")
    image.pop("os_build_name", None)
    image.pop("os_build", None)
    if existing_osb is not None:
        conflicts = _conflicting_osbas(handler, existing_osb, image)
        if conflicts:
            if not _STATE["replace"]:
                raise NeedsReplace(existing_osb, conflicts)
            for attr in conflicts:
                _STATE["removed"].append(_describe_osba(attr))
                handler.delete_osbuild_attribute(attr)
        # AWS and Azure add_template() require os_build_name positionally and reset os_build
        # unless the mapping flag says the caller chose the build on purpose.
        return handler.add_template(os_build_name=existing_osb.name, os_build=existing_osb,
                                    is_ami_osb_mapping=True, is_images_osb_mapping=True, **image)
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
    if not os_build_name:
        raise Choices(_aws_rows(cache.filter(ami_id=ami_id)), "One AMI matches ({}); pass onb_os_build_name to import it.".format(ami_id))
    already = AmazonMachineImage.objects.filter(resourcehandler=handler, ami_id=ami_id, region=region).order_by("-id").first()
    if already is not None and already.os_build is not None:
        return already.os_build, None
    # An AMI stored by an earlier run of this tool has no region, so the environment's lookup
    # (AWSHandler.get_osba_for_osb_and_region) cannot see it. Give it the region and reuse it.
    stale = AmazonMachineImage.objects.filter(resourcehandler=handler, ami_id=ami_id, region="").order_by("-id").first()
    if stale is not None and stale.os_build is not None:
        AmazonMachineImage.objects.filter(pk=stale.pk).update(region=region)
        _progress(job, "Set region {} on the image {} imported earlier without one".format(region, ami_id))
        return stale.os_build, None
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
    # AWSHandler.add_template_attrs creates the record without its region; CloudBolt's own
    # import (resourcehandlers/aws/services.py) fills it afterwards from the image dict. The
    # region is what an environment uses to find the image, so set it here the same way.
    fields = {"region": region}
    if isinstance(image_dict.get("description"), str) and image_dict["description"]:
        fields["description"] = image_dict["description"]
    if isinstance(image_dict.get("owner_alias"), str) and image_dict["owner_alias"]:
        fields["owner_alias"] = image_dict["owner_alias"][:100]
    AmazonMachineImage.objects.filter(pk=osba.pk).update(**fields)
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
    if not os_build_name:
        raise Choices(_azure_rows([match]), "One image matches ({}); pass onb_os_build_name to import it.".format(template_name))
    already = AzureARMImage.objects.filter(resourcehandler=handler, template_name=template_name, region=location).order_by("-id").first()
    if already is not None and already.os_build is not None:
        return already.os_build, None
    if existing_osb is not None:
        conflicts = _conflicting_osbas(handler, existing_osb, {"name": template_name, "region": location})
        if conflicts:
            if not _STATE["replace"]:
                raise NeedsReplace(existing_osb, conflicts)
            for attr in conflicts:
                _STATE["removed"].append(_describe_osba(attr))
                handler.delete_osbuild_attribute(attr)
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
    if not os_build_name:
        raise Choices(_vmware_rows([match]), "One template matches ({}); pass onb_os_build_name to import it.".format(match["name"]))
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
    if not os_build_name:
        raise Choices([{"name": candidates[0].get("name") or candidates[0].get("value")}], "One image matches; pass onb_os_build_name to import it.")
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
    _STATE["replace"] = inputs["replace_existing"]
    _STATE["removed"] = []

    try:
        if slug.startswith("aws"):
            osb, error = _import_aws(handler, inputs["image"], location, inputs["os_build_name"], existing_osb, job)
        elif slug == "azure_arm":
            osb, error = _import_azure(handler, inputs["image"], location, inputs["os_build_name"], existing_osb, job)
        elif slug == "vmware":
            osb, error = _import_vmware(handler, inputs["image"], inputs["os_build_name"], existing_osb, job)
        else:
            osb, error = _import_generic(handler, inputs["image"], inputs["os_build_name"], existing_osb, job)
    except NeedsReplace as conflict:
        existing = [_describe_osba(attr) for attr in conflict.attrs]
        outputs = {
            "handler": {"id": handler.global_id, "name": handler.name, "technologySlug": slug},
            "image": inputs["image"],
            "location": location,
            "confirmationRequired": True,
            "osBuild": {"id": conflict.osb.global_id, "name": conflict.osb.name},
            "existingImages": existing,
            "next": (
                "Nothing changed. CloudBolt allows one image per OS build per handler{}. Ask the user whether to replace "
                "{} with '{}' in '{}'. Yes: call again with onb_replace_existing=true, which removes the existing image "
                "first. No: use another onb_os_build_name.".format(
                    " in each region" if slug.startswith("aws") or slug == "azure_arm" else "",
                    ", ".join("'{}'".format(row["template"]) for row in existing),
                    inputs["image"], conflict.osb.name,
                )
            ),
        }
        return _success(
            "Confirmation needed: OS build '{}' already holds {} on {}.".format(
                conflict.osb.name, ", ".join("'{}'".format(row["template"]) for row in existing), handler.name),
            outputs,
        )
    except Choices as choices:
        outputs = {
            "handler": {"id": handler.global_id, "name": handler.name, "technologySlug": slug},
            "location": location,
            "filter": inputs["image"] or None,
            "images": choices.rows,
            "note": choices.note,
            "next": "Call again with onb_image set to one AMI id, template name or publisher:offer:sku:version from the list, and onb_os_build_name.",
        }
        if inputs["image"] and inputs["os_build_name"]:
            return {"status": "FAILURE", "output_message": "", "error_message": choices.note, "outputs": outputs}
        return _success(choices.note, outputs)
    except Exception as exc:
        return _failure("Importing '{}' on {} failed: {}: {}".format(inputs["image"], handler.name, type(exc).__name__, str(exc)[:600]))
    if error:
        return _failure(error)

    attached = []
    unresolved = []
    for env in envs:
        if not osb.environments.filter(id=env.id).exists():
            osb.environments.add(env)
        attached.append({"id": env.global_id, "name": env.name})
        try:
            resolved = osb.osba_for_resource_handler(handler, environment=env)
        except Exception as exc:
            resolved = None
            unresolved.append("{}: {}".format(env.name, str(exc)[:300]))
        else:
            if resolved is None:
                unresolved.append("{}: no image of OS build '{}' for this environment".format(env.name, osb.name))

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
    if _STATE["removed"]:
        outputs["replacedImages"] = list(_STATE["removed"])
    outputs["verified"] = not unresolved
    if unresolved:
        outputs["unresolved"] = unresolved
        message = (
            "OS build '{}' is attached, but CloudBolt cannot resolve its image for {} environment(s), so an order there "
            "would fail: {}. Run this tool again for the image (it repairs an image stored without a region), or check "
            "the image's region against the environment's.".format(osb.name, len(unresolved), "; ".join(unresolved))
        )
        return {"status": "FAILURE", "output_message": "", "error_message": message, "outputs": outputs}
    return _success(
        "OS build '{}' holds {} and is attached to {} environment(s); verified that each environment resolves the image.".format(
            osb.name, inputs["image"], len(attached)),
        outputs,
    )
