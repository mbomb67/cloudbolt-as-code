"""
POC Onboarding: Environment Networks (MCP Tool Action plugin).

Lists the networks an environment can offer on its first NIC and, when
onb_networks names up to five, makes exactly those the NIC 1 choices on the
order form: it adds the chosen ones, removes the others from NIC 1, and
removes networks from every other NIC so servers get one NIC. Each network
row shows its addressing (DHCP, static, IP pool, IPAM) so the next step can
configure it with onb_network_addressing. Admin only.

Networks are synced at the handler level for vCenter, SCVMM, Hyper-V,
Nutanix, H3C CAS and OpenStack, and per environment for AWS, Azure, Azure
Local, OLVM, OpenShift Virtualization, vCloud Director and Outscale
(Environment.get_possible_networks and the handler's sync methods). A subnet
re-sync puts every subnet back on NIC 1, so onb_refresh re-applies the
selection afterwards when onb_networks is given.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
MAX_NETWORKS = 5
MAX_LIST = 200

# What addressing means per technology (from the CloudBolt source of each handler).
NO_NETWORKS = {"proxmox", "metalsoft", "orka", "ipmi"}
ADDRESSING = {"vmware", "vmc_aws", "scvmm", "hyperv", "acropolis", "h3ccas", "vcloud_director", "azurelocal"}
CLOUD_ASSIGNS = {"aws", "aws_govcloud", "aws_china", "awsjit", "azure_arm", "oci", "openstack"}
NO_EFFECT = {"gcp", "alibaba", "outscale", "ibm_cloud", "ibm_gov", "slayer", "maas", "ovirt", "olvm"}


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


def _progress(job, message):
    if job is None:
        return
    try:
        from common.methods import set_progress

        set_progress(message)
    except Exception:
        pass


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
        "onb_environment": """{{ onb_environment }}""",
        "onb_networks": """{{ onb_networks }}""",
        "onb_refresh": """{{ onb_refresh }}""",
    }
    return {
        "environment": _text(raw["onb_environment"]),
        "networks": _lines(raw["onb_networks"]),
        "refresh": _text(raw["onb_refresh"]).lower() in ("true", "1", "yes", "y"),
    }


def _resolve_env(ref):
    from infrastructure.models import Environment

    env = None
    if GLOBAL_ID_RE.match(ref):
        env = Environment.objects.filter(global_id=ref).first()
    elif ref.isdigit():
        env = Environment.objects.filter(id=int(ref)).first()
    if env is None:
        matches = list(Environment.objects.filter(name__iexact=ref))
        if len(matches) > 1:
            return None, "Several environments are named '{}'; pass the ENV- global id.".format(ref)
        env = matches[0] if matches else None
    if env is None:
        return None, "No environment matches '{}'.".format(ref)
    return env, None


def _tech_slug(handler):
    tech = getattr(handler, "resource_technology", None)
    if tech is None:
        return ""
    return (getattr(tech, "slug", None) or getattr(tech, "type_slug", "") or "").strip()


def _category(slug):
    if slug in NO_NETWORKS:
        return "none"
    if slug in ADDRESSING:
        return "addressing"
    if slug in CLOUD_ASSIGNS:
        return "cloud_assigns"
    if slug in NO_EFFECT:
        return "no_effect"
    return "addressing"


def _nic_networks(env, position):
    """Networks on NIC <position> (1-based) of the environment, cast to their subclass."""
    field = "sc_nic_{}".format(position - 1)
    rows = []
    for cfv in env.custom_field_options.filter(field__name=field).select_related("network_value"):
        net = cfv.network_value
        if net is None:
            continue
        try:
            net = net.cast()
        except Exception:
            pass
        rows.append(net)
    return rows


def _other_nic_positions(env):
    positions = set()
    for name in env.custom_field_options.filter(field__name__regex=r"^sc_nic_[0-9]+$").values_list("field__name", flat=True):
        index = int(name.rsplit("_", 1)[1])
        if index >= 1:
            positions.add(index + 1)
    return sorted(positions)


def _network_row(net):
    try:
        provider = net.determine_ip_provider()
    except Exception:
        provider = None
    row = {
        "id": getattr(net, "global_id", None),
        "pk": net.pk,
        "name": net.name,
        "network": getattr(net, "network", None),
        "vlan": getattr(net, "vlan", None),
        "addressing": getattr(net, "addressing_schema", None),
        "ipProvider": provider,
        "gateway": getattr(net, "gateway", None),
        "netmask": getattr(net, "netmask", None),
        "dns1": getattr(net, "dns1", None),
        "dns2": getattr(net, "dns2", None),
        "dnsDomain": getattr(net, "dns_domain", None),
    }
    try:
        pool = net.ip_pool_string
        if pool:
            row["ipPool"] = pool
    except Exception:
        pass
    if getattr(net, "ipam_network_id", None):
        row["ipamNetwork"] = str(net.ipam_network)
    return row


def _refresh(handler, env, job):
    """Re-sync networks: per environment where the technology imports subnets there, else per handler."""
    try:
        per_env = bool(handler.can_import_subnets_on_environments(env))
    except Exception:
        per_env = False
    if per_env and hasattr(handler, "sync_subnets"):
        _progress(job, "Syncing the environment's subnets")
        handler.sync_subnets(env)
        return "environment subnets synced"
    if hasattr(handler, "sync_networks"):
        _progress(job, "Syncing the handler's networks")
        handler.sync_networks()
        return "handler networks synced"
    return "this technology has no network sync"


def _match(ref, candidates):
    ref_l = ref.lower()
    for net in candidates:
        if ref == (getattr(net, "global_id", None) or "") or ref == str(net.pk):
            return net
    exact = [n for n in candidates if (n.name or "").lower() == ref_l]
    if len(exact) == 1:
        return exact[0]
    ident = [n for n in candidates if (getattr(n, "network", "") or "").lower() == ref_l]
    if len(ident) == 1:
        return ident[0]
    if len(exact) > 1 or len(ident) > 1:
        raise ValueError("Several networks match '{}'; pass the NET- id.".format(ref))
    return None


def _next_step(category):
    if category == "addressing":
        return ("For each NIC 1 network, set how servers get their IP with onb_network_addressing: DHCP, or static "
                "with gateway and netmask (and DNS), then an IP pool, an IPAM or an IP typed on each order.")
    if category == "cloud_assigns":
        return "The cloud assigns the IP on these networks; no addressing setup is needed. An order may still pass a static private IP in sc_nic_0_ip."
    if category == "no_effect":
        return "The cloud assigns the IP on these networks and CloudBolt sends no addressing; no addressing setup is needed."
    return ""


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may change environment networks.")

    inputs = _read_inputs()
    if not inputs["environment"]:
        return _failure("onb_environment is required: the ENV- global id or the environment name.")
    env, error = _resolve_env(inputs["environment"])
    if error:
        return _failure(error)
    handler = env.resource_handler.cast() if env.resource_handler else None
    if handler is None:
        return _failure("Environment '{}' has no resource handler.".format(env.name))
    slug = _tech_slug(handler)
    category = _category(slug)
    base = _portal_url()
    outputs = {
        "environment": {"id": env.global_id, "name": env.name, "url": base + "/environments/{}/".format(env.id)},
        "handler": {"id": handler.global_id, "name": handler.name, "technologySlug": slug},
        "category": category,
    }
    if category == "none":
        outputs["note"] = "This technology has no CloudBolt networks; there is nothing to choose or configure."
        return _success("{}: no networks for this technology.".format(env.name), outputs)

    if len(inputs["networks"]) > MAX_NETWORKS:
        return _failure("Choose at most {} networks for NIC 1; {} were given.".format(MAX_NETWORKS, len(inputs["networks"])))

    if inputs["refresh"]:
        try:
            outputs["refresh"] = _refresh(handler, env, job)
        except Exception as exc:
            return _failure("Refreshing the networks of '{}' failed: {}: {}".format(env.name, type(exc).__name__, str(exc)[:400]))

    from resourcehandlers.models import ResourceNetwork

    nic1 = _nic_networks(env, 1)
    nic1_ids = {n.pk for n in nic1}
    possible_ids = set(env.get_possible_networks().values_list("pk", flat=True))
    candidates = list(nic1)
    for net in ResourceNetwork.objects.filter(pk__in=possible_ids - nic1_ids).order_by("name")[:MAX_LIST]:
        try:
            candidates.append(net.cast())
        except Exception:
            candidates.append(net)

    if not inputs["networks"]:
        available = [_network_row(n) for n in candidates if n.pk not in nic1_ids]
        outputs.update(
            {
                "nic1": [_network_row(n) for n in nic1],
                "available": available,
                "otherNics": _other_nic_positions(env),
                "next": "Ask which networks (up to {}) servers in this environment should use, then call again with onb_networks.".format(MAX_NETWORKS),
            }
        )
        if slug == "azure_arm" and not available:
            outputs["note"] = ("Azure offers only subnets already on the environment. To get others back, call with "
                               "onb_refresh=true (it re-imports every subnet of the region) and onb_networks set.")
        if not nic1 and not available:
            outputs["note"] = "No networks are known for this environment yet; call again with onb_refresh=true to sync them."
        return _success("{}: {} network(s) on NIC 1, {} more available.".format(env.name, len(nic1), len(available)), outputs)

    chosen = []
    for ref in inputs["networks"]:
        try:
            net = _match(ref, candidates)
        except ValueError as exc:
            return _failure(str(exc))
        if net is None:
            return _failure("Network '{}' is not available to environment '{}'. Call without onb_networks to list them{}.".format(
                ref, env.name, ", or with onb_refresh=true to sync them first" if not inputs["refresh"] else ""))
        if net.pk not in possible_ids and net.pk not in nic1_ids:
            return _failure("Network '{}' cannot be used by environment '{}'.".format(net.name, env.name))
        if net.pk not in {c.pk for c in chosen}:
            chosen.append(net)

    chosen_ids = {n.pk for n in chosen}
    added, removed, other_removed = [], [], []
    for net in chosen:
        if net.pk not in nic1_ids:
            env.add_network(net, nic_position=1)
            added.append(net.name)
    for net in nic1:
        if net.pk not in chosen_ids:
            env.remove_network(net, nic_position=1)
            removed.append(net.name)
    for position in _other_nic_positions(env):
        for net in _nic_networks(env, position):
            env.remove_network(net, nic_position=position)
            other_removed.append("NIC {}: {}".format(position, net.name))

    after = _nic_networks(env, 1)
    after_ids = {n.pk for n in after}
    problems = []
    if after_ids != chosen_ids:
        problems.append("NIC 1 holds {} instead of the chosen networks.".format(", ".join(n.name for n in after) or "nothing"))
    possible_after = set(env.get_possible_networks().values_list("pk", flat=True))
    for net in after:
        if net.pk not in possible_after:
            problems.append("'{}' is on NIC 1 but the environment cannot use it; the order form would reject it.".format(net.name))
    if _other_nic_positions(env):
        problems.append("Networks remain on another NIC.")

    outputs.update(
        {
            "nic1": [_network_row(n) for n in after],
            "added": added,
            "removedFromNic1": removed,
            "removedFromOtherNics": other_removed,
            "verified": not problems,
            "next": _next_step(category),
        }
    )
    if problems:
        outputs["problems"] = problems
        return {"status": "FAILURE", "output_message": "", "error_message": " ".join(problems), "outputs": outputs}
    return _success(
        "{}: NIC 1 now offers {} ({} added, {} removed); verified.".format(
            env.name, ", ".join(n.name for n in after), len(added), len(removed) + len(other_removed)),
        outputs,
    )
