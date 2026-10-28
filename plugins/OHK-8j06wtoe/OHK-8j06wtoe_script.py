"""
POC Onboarding: Import Environments (MCP Tool Action plugin).

Creates one environment per listed location on a resource handler, the
same way the handler page's Import locations dialog does
(create_location_specific_env), then brings the networks over: AWS VPC
subnets are imported with the environment and Azure subnets are synced.
vCenter networks are not attached here: the user chooses them, up to five,
with onb_environment_networks. An AWS region creates one environment for
one VPC: the region alone works only when it has a single VPC, otherwise
the user picks it. A handler's first AWS environment also adds the Name and
Annotation tags the handler form would have added. A location that
already has an environment is reported, not duplicated. Runs as a job
because it calls the cloud API. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
MAX_LOCATIONS = 4
SPECIAL_FIELD = {
    "aws": "aws_region",
    "aws_govcloud": "aws_region",
    "aws_china": "aws_region",
    "azure_arm": "node_location",
    "vmware": "vmware_cluster",
    "scvmm": "scvmm_host_cluster",
    "gcp": "gcp_project",
    "acropolis": "node_location",
    "openstack": "node_location",
}


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
        "onb_env_name": """{{ onb_env_name }}""",
    }
    return {
        "handler": _text(raw["onb_handler"]),
        "locations": _lines(raw["onb_locations"]),
        "env_name": _text(raw["onb_env_name"]),
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


def _find_env(handler, field, value, vpc_id=None):
    for env in handler.environment_set.all().order_by("id"):
        try:
            current = env.get_cfv(field)
        except Exception:
            current = None
        if current is None or str(current) != str(value):
            continue
        if vpc_id:
            try:
                if str(env.get_cfv("vpc_id") or "") != vpc_id:
                    continue
            except Exception:
                continue
        return env
    return None


def _env_row(env, base, status, **extra):
    try:
        networks = len(env.networks())
    except Exception:
        networks = None
    row = {
        "environmentId": env.global_id,
        "pk": env.id,
        "name": env.name,
        "status": status,
        "networks": networks,
        "osBuilds": env.os_builds.count(),
        "url": base + "/environments/{}/".format(env.id),
        "parametersUrl": base + "/environments/{}/#tab-parameters".format(env.id),
    }
    row.update(extra)
    return row


def _vpc_rows(handler, region):
    """
    The region's VPCs with their default flag, CIDR and subnet count.
    get_vpc_dicts_for_region returns only id and name, so read the EC2 Vpc
    resources through the handler's own boto3 helper, the same call it makes.
    Vpc attributes: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2/vpc/index.html
    """
    try:
        handler.connect(region_name=region)
        ec2 = handler.resource_technology.work_class.get_boto3_resource(
            handler.serviceaccount, handler.servicepasswd, region
        )
        rows = []
        for vpc in ec2.vpcs.all():
            name = ""
            for tag in vpc.tags or []:
                if tag.get("Key") == "Name":
                    name = tag.get("Value") or ""
            try:
                subnets = len(list(vpc.subnets.all()))
            except Exception:
                subnets = None
            rows.append(
                {"id": vpc.id, "name": name, "cidr": vpc.cidr_block, "isDefault": bool(vpc.is_default), "subnets": subnets}
            )
        return rows
    except Exception:
        return [
            {"id": v.get("id"), "name": v.get("name") if v.get("name") != v.get("id") else "", "cidr": None,
             "isDefault": False, "subnets": None}
            for v in handler.get_vpc_dicts_for_region(region)
            if isinstance(v, dict)
        ]


def _vpc_list(rows):
    return ", ".join(
        "{} ({}{}{})".format(
            r["id"],
            r["name"] or "unnamed",
            ", " + r["cidr"] if r.get("cidr") else "",
            ", default" if r.get("isDefault") else "",
        )
        for r in rows
    )


def _pick_vpc(handler, region, wanted):
    """
    The VPC to build one environment for. With the region alone it picks the
    VPC only when the region has exactly one; with several the user chooses,
    because CloudBolt's own import makes one environment per VPC.
    """
    rows = _vpc_rows(handler, region)
    if not rows:
        return None, "Region {} has no VPC visible to these credentials.".format(region)
    if wanted:
        match = None
        for row in rows:
            if row["id"] == wanted or (row["name"] and row["name"].lower() == wanted.lower()):
                match = row
                break
        if match is None:
            return None, "No VPC '{}' in region {}. Available: {}.".format(wanted, region, _vpc_list(rows))
    elif len(rows) == 1:
        match = rows[0]
    else:
        return None, (
            "Region {} has {} VPCs; ask the user which one the POC uses and pass <region>|<vpc-id>. "
            "Available: {}.".format(region, len(rows), _vpc_list(rows))
        )
    if match.get("subnets") == 0:
        return None, "VPC {} in {} has no subnets, so no server can be built there. Available: {}.".format(
            match["id"], region, _vpc_list(rows))
    return match, None


def _add_default_tags(handler):
    """
    The Name and Annotation tags that the handler form's Done adds when its
    checkbox is left on; the POC flow closes that screen instead of clicking
    Done, so the first environment adds them (the form's add_default_tag_attributes).
    """
    from tags.models import TaggableAttribute

    added = []
    for attribute, label in (("hostname", "Name"), ("annotation", "Annotation")):
        _ta, created = TaggableAttribute.objects.get_or_create(resource_handler=handler, attribute=attribute, label=label)
        if created:
            added.append(label)
    return added


def _import_aws(handler, line, prefix, base, job):
    region, _sep, wanted_vpc = line.partition("|")
    region = region.strip()
    wanted_vpc = wanted_vpc.strip()
    vpc, error = _pick_vpc(handler, region, wanted_vpc)
    if error:
        return {"location": line, "status": "failed", "error": error}
    existing = _find_env(handler, "aws_region", region, vpc_id=vpc["id"])
    if existing is not None:
        return _env_row(existing, base, "existing", location=region, vpc=vpc["id"])
    vpc_label = vpc["name"] or vpc["id"]
    env_name = ""
    if prefix:
        env_name = "{} {}".format(prefix, region)
        if vpc_label.lower() != region.lower():
            env_name += " " + vpc_label
    first = not handler.environment_set.exists()
    _progress(job, "Creating AWS environment for {} / {}".format(region, vpc["id"]))
    env = handler.create_location_specific_env(region, env_name=env_name, vpc_id=vpc["id"], vpc_name=vpc["name"] or "")
    extra = {"location": region, "vpc": vpc["id"], "vpcCidr": vpc.get("cidr")}
    if first:
        try:
            extra["tagsAdded"] = _add_default_tags(handler)
        except Exception as exc:
            extra["tagsError"] = str(exc)[:300]
    return _env_row(env, base, "created", **extra)


def _import_azure(handler, location, prefix, base, job):
    existing = _find_env(handler, "node_location", location)
    if existing is not None:
        return _env_row(existing, base, "existing", location=location)
    env_name = "{} {}".format(prefix, location).strip() if prefix else "({}) {}".format(handler.name, location)
    _progress(job, "Creating Azure environment for {}".format(location))
    env = handler.create_location_specific_env(location, env_name=env_name)
    note = "Azure imports the environment's parameters (sizes, resource groups, storage) in a separate job that runs now; give it a minute before trimming the order form."
    try:
        imported, deleted = handler.sync_subnets(env)
        subnets = {"imported": imported, "deleted": deleted}
    except Exception as exc:
        subnets = {"error": str(exc)[:300]}
    return _env_row(env, base, "created", location=location, subnets=subnets, note=note)


def _import_vmware(handler, cluster, prefix, base, job):
    existing = _find_env(handler, "vmware_cluster", cluster)
    if existing is not None:
        env = existing
        status = "existing"
    else:
        env_name = "{} {}".format(prefix, cluster).strip() if prefix else ""
        _progress(job, "Creating vCenter environment for cluster {}".format(cluster))
        env = handler.create_location_specific_env(cluster, env_name=env_name)
        status = "created"
    note = ("Choose the networks servers in this environment use (up to five) with onb_environment_networks, then set "
            "each one's addressing with onb_network_addressing.")
    return _env_row(env, base, status, location=cluster, note=note)


def _import_generic(handler, slug, location, prefix, base, job):
    field = SPECIAL_FIELD.get(slug)
    if field:
        existing = _find_env(handler, field, location)
        if existing is not None:
            return _env_row(existing, base, "existing", location=location)
    env_name = "{} {}".format(prefix, location).strip() if prefix else ""
    extra = {}
    if slug == "acropolis":
        try:
            _total, not_in_cb, _only = handler.import_locations_helper()
            for item in not_in_cb:
                if isinstance(item, dict) and item.get("name") == location and item.get("uuid"):
                    extra["cluster_uuid"] = item["uuid"]
        except Exception:
            pass
    _progress(job, "Creating environment for {}".format(location))
    env = handler.create_location_specific_env(location, env_name=env_name, **extra)
    return _env_row(env, base, "created", location=location)


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may import environments.")

    inputs = _read_inputs()
    if not inputs["handler"]:
        return _failure("onb_handler is required: the handler's RH- global id or its name.")
    if not inputs["locations"]:
        return _failure("onb_locations is required: one location per line (AWS: <region> or <region>|<vpc-id>).")
    if len(inputs["locations"]) > MAX_LOCATIONS:
        return _failure("At most {} locations per call; a POC needs one environment per handler.".format(MAX_LOCATIONS))
    handler, error = _resolve_handler(inputs["handler"])
    if error:
        return _failure(error)

    base = _portal_url()
    tech = handler.resource_technology
    slug = tech.slug if tech else ""
    results = []
    for line in inputs["locations"]:
        try:
            if slug.startswith("aws"):
                results.append(_import_aws(handler, line, inputs["env_name"], base, job))
            elif slug == "azure_arm":
                results.append(_import_azure(handler, line, inputs["env_name"], base, job))
            elif slug == "vmware":
                results.append(_import_vmware(handler, line, inputs["env_name"], base, job))
            else:
                results.append(_import_generic(handler, slug, line, inputs["env_name"], base, job))
        except Exception as exc:
            results.append(
                {
                    "location": line,
                    "status": "failed",
                    "error": "{}: {}".format(type(exc).__name__, str(exc)[:600]),
                    "hint": (
                        "Check the location name against onb_handler_locations, then onb_recent_errors with "
                        "onb_minutes=5 for the traceback."
                    ),
                }
            )

    created = sum(1 for r in results if r.get("status") == "created")
    existing = sum(1 for r in results if r.get("status") == "existing")
    failed = sum(1 for r in results if r.get("status") == "failed")
    outputs = {
        "handler": {"id": handler.global_id, "name": handler.name, "technologySlug": slug},
        "results": results,
        "next": (
            "For each environment: onb_import_os_build to attach an image, then onb_trim_environment to shorten "
            "the order form, then onb_configure_custom_server."
        ),
    }
    message = "{}: {} environment(s) created, {} existing, {} failed.".format(handler.name, created, existing, failed)
    if failed and not created and not existing:
        return {"status": "FAILURE", "output_message": "", "error_message": message, "outputs": outputs}
    return _success(message, outputs)
