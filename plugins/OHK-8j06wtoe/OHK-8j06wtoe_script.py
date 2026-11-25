"""
POC Onboarding: Import Environments (MCP Tool Action plugin).

Creates one environment per listed location on a resource handler, the
same way the handler page's Import locations dialog does
(create_location_specific_env), then brings the networks over: AWS VPC
subnets are imported with the environment, Azure subnets are synced, and
vCenter networks of the cluster are attached to NIC 1. A location that
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
MAX_VMWARE_NETWORKS = 20
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


def _pick_vpc(handler, region, wanted):
    vpcs = [v for v in handler.get_vpc_dicts_for_region(region) if isinstance(v, dict)]
    rows = []
    for vpc in vpcs:
        rows.append(
            {
                "id": vpc.get("id") or vpc.get("VpcId") or vpc.get("vpc_id"),
                "name": vpc.get("name") or vpc.get("Name") or "",
                "isDefault": bool(vpc.get("is_default") or vpc.get("IsDefault") or vpc.get("default")),
            }
        )
    if not rows:
        return None, "Region {} has no VPC visible to these credentials.".format(region)
    if wanted:
        for row in rows:
            if row["id"] == wanted or (row["name"] and row["name"].lower() == wanted.lower()):
                return row, None
        return None, "No VPC '{}' in region {}. Available: {}.".format(
            wanted, region, ", ".join("{} ({})".format(r["id"], r["name"] or "unnamed") for r in rows)
        )
    for row in rows:
        if row["isDefault"]:
            return row, None
    if len(rows) == 1:
        return rows[0], None
    return None, "Region {} has {} VPCs and none is the default; pass <region>|<vpc-id>. Available: {}.".format(
        region, len(rows), ", ".join("{} ({})".format(r["id"], r["name"] or "unnamed") for r in rows)
    )


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
    _progress(job, "Creating AWS environment for {} / {}".format(region, vpc["id"]))
    env = handler.create_location_specific_env(region, env_name=env_name, vpc_id=vpc["id"], vpc_name=vpc["name"] or "")
    return _env_row(env, base, "created", location=region, vpc=vpc["id"])


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
    attached = []
    try:
        if not env.networks():
            _progress(job, "Syncing vCenter networks")
            try:
                handler.sync_networks()
            except Exception as exc:
                attached.append("network sync failed: {}".format(str(exc)[:200]))
            for network in list(handler.get_possible_networks(env))[:MAX_VMWARE_NETWORKS]:
                env.add_network(network, nic_position=1)
                attached.append(network.name)
    except Exception as exc:
        attached.append("network attach failed: {}".format(str(exc)[:200]))
    return _env_row(env, base, status, location=cluster, networksAttached=attached)


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
        return _failure("At most {} locations per call; a POC needs one or two environments per handler.".format(MAX_LOCATIONS))
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
