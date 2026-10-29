"""
POC Onboarding: Handler Locations (MCP Tool Action plugin).

Lists what a resource handler can turn into environments: AWS regions (and
the VPCs of one region when onb_region is given), Azure locations, vCenter
or SCVMM or Nutanix clusters, GCP projects, OpenStack locations. Each entry
says whether an environment already exists for it, and the existing
environments are listed, so the agent can offer the user one or two
locations and then call onb_import_environments. Runs as a job because it
calls the cloud API. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
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
MAX_VPCS = 50


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
        "onb_region": """{{ onb_region }}""",
    }
    return {"handler": _text(raw["onb_handler"]), "region": _text(raw["onb_region"])}


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


def _env_location(env, field):
    if not field:
        return None
    try:
        value = env.get_cfv(field)
    except Exception:
        return None
    return str(value) if value is not None else None


def _existing_environments(handler, slug, base):
    field = SPECIAL_FIELD.get(slug)
    rows = []
    for env in handler.environment_set.all().order_by("id"):
        row = {
            "id": env.global_id,
            "name": env.name,
            "location": _env_location(env, field),
            "url": base + "/environments/{}/".format(env.id),
        }
        if slug.startswith("aws"):
            row["vpc"] = _env_location(env, "vpc_id")
        rows.append(row)
    return rows


def _name_of(item):
    if isinstance(item, dict):
        return item.get("name") or item.get("ClusterName") or item.get("id") or str(item)
    return str(item)


def _display_of(item):
    if isinstance(item, dict):
        return item.get("display") or item.get("display_name") or item.get("displayName") or item.get("ClusterName") or item.get("name")
    return str(item)


def _rows(total, not_in_cb):
    pending = {_name_of(x) for x in not_in_cb}
    rows = []
    for item in total:
        name = _name_of(item)
        row = {"name": name, "display": _display_of(item), "imported": name not in pending}
        if isinstance(item, dict) and item.get("uuid"):
            row["uuid"] = item["uuid"]
        rows.append(row)
    rows.sort(key=lambda r: (r["imported"], r["name"]))
    return rows


def _vpcs(handler, region):
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
        for vpc in list(ec2.vpcs.all())[:MAX_VPCS]:
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
            for v in list(handler.get_vpc_dicts_for_region(region))[:MAX_VPCS]
            if isinstance(v, dict)
        ]


def _recommended_vpc(rows):
    """The VPC to offer first: the only one with subnets, else the default VPC when it has subnets."""
    usable = [r for r in rows if r.get("subnets") != 0]
    if len(usable) == 1:
        return usable[0]["id"]
    for row in usable:
        if row.get("isDefault"):
            return row["id"]
    return None


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may list handler locations.")

    inputs = _read_inputs()
    if not inputs["handler"]:
        return _failure("onb_handler is required: the handler's RH- global id or its name.")
    handler, error = _resolve_handler(inputs["handler"])
    if error:
        return _failure(error)

    base = _portal_url()
    tech = handler.resource_technology
    slug = tech.slug if tech else ""
    outputs = {
        "handler": {"id": handler.global_id, "name": handler.name, "technology": tech.name if tech else None, "technologySlug": slug},
        "existingEnvironments": _existing_environments(handler, slug, base),
        "importLocationsUrl": base + "/admin/resourcehandlers/{}/import_locations/".format(handler.id),
    }
    _progress(job, "Listing locations of {} ({})".format(handler.name, slug))
    try:
        if slug.startswith("aws"):
            total, not_in_cb, _only = handler.import_regions_helper()
            outputs["locationKind"] = "region"
            outputs["locations"] = _rows(total, not_in_cb)
            if inputs["region"]:
                outputs["region"] = inputs["region"]
                vpcs = _vpcs(handler, inputs["region"])
                outputs["vpcs"] = vpcs
                outputs["recommendedVpc"] = _recommended_vpc(vpcs)
                if len(vpcs) == 1:
                    outputs["next"] = (
                        "The region has one VPC: call onb_import_environments with onb_locations {}|{}.".format(
                            inputs["region"], vpcs[0]["id"])
                    )
                else:
                    outputs["next"] = (
                        "Ask the user which one VPC the POC uses (offer recommendedVpc first; a VPC with no subnets "
                        "cannot build servers), then call onb_import_environments with <region>|<vpc-id> for that VPC only."
                    )
            else:
                outputs["next"] = (
                    "Pick one region. Call this tool again with onb_region to list that region's VPCs, then import the "
                    "one VPC the POC uses with onb_import_environments (<region>|<vpc-id>)."
                )
        elif slug == "azure_arm":
            total, not_in_cb, _only = handler.import_locations_helper()
            outputs["locationKind"] = "location"
            outputs["locations"] = _rows(total, not_in_cb)
            if not (handler.serviceaccount or "").strip():
                try:
                    subs = handler.get_azure_subscriptions()
                    outputs["subscriptions"] = [
                        {"id": s.get("subscription_id") or s.get("id"), "name": s.get("display_name") or s.get("name")}
                        for s in list(subs)[:50]
                        if isinstance(s, dict)
                    ]
                    outputs["note"] = (
                        "This handler has no subscription of its own; import a subscription first on the handler page "
                        "(Import Azure Subscriptions), then import locations on the subscription's handler."
                    )
                except Exception as exc:
                    outputs["subscriptionsError"] = str(exc)[:300]
            outputs["next"] = "Pick one or two locations and call onb_import_environments with them, one per line."
        elif slug == "vmware":
            total, not_in_cb, _only = handler.import_clusters_helper()
            outputs["locationKind"] = "cluster"
            outputs["locations"] = _rows(total, not_in_cb)
            outputs["next"] = "Pick one cluster and call onb_import_environments with its name."
        elif slug == "scvmm":
            all_clusters = handler.get_all_clusters()
            current = set(handler.current_clusters())
            outputs["locationKind"] = "host cluster"
            outputs["locations"] = [
                {"name": c.get("ClusterName"), "display": c.get("ClusterName"), "imported": c.get("ClusterName") in current}
                for c in all_clusters
                if isinstance(c, dict)
            ]
            outputs["next"] = "Pick one host cluster and call onb_import_environments with its name."
        else:
            total, not_in_cb, _only = handler.import_locations_helper()
            outputs["locationKind"] = "location"
            outputs["locations"] = _rows(total, not_in_cb)
            outputs["next"] = "Pick one or two locations and call onb_import_environments with them, one per line."
    except AttributeError as exc:
        return _failure(
            "This technology ({}) does not expose a location list to import from; create the environment on the "
            "handler page instead. Detail: {}".format(slug or "unknown", str(exc)[:200])
        )
    except Exception as exc:
        return _failure(
            "Listing locations of '{}' failed: {}: {}. Run onb_verify_resource_handler; the credentials may "
            "authenticate but lack a describe or list permission.".format(handler.name, type(exc).__name__, str(exc)[:600])
        )

    count = len(outputs.get("locations") or [])
    imported = sum(1 for r in outputs.get("locations") or [] if r.get("imported"))
    message = "{}: {} {}(s), {} already imported, {} environment(s).".format(
        handler.name, count, outputs.get("locationKind", "location"), imported, len(outputs["existingEnvironments"])
    )
    return _success(message, outputs)
