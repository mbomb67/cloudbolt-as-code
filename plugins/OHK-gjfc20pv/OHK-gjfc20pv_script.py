"""
POC Onboarding: Network Addressing (MCP Tool Action plugin).

Sets how servers on one CloudBolt network get their IP address, the way the
network's edit page and its IP Pool tab do, and proves the result with
ResourceNetwork.determine_ip_provider(), the call the order form and the
provisioning job use:

- DHCP: addressing schema dhcp.
- Static: addressing schema static, with gateway and netmask required
  (vCenter refuses a static NIC 1 without both) and DNS 1, DNS 2 and DNS
  domain optional; then where the addresses come from:
  - pool: a CloudBolt IP pool on the network (ResourceNetwork.add_ip_range or
    add_ip_addresses), checked for free addresses;
  - ipam: an IPAM endpoint the user added in the UI (credentials stay there);
    the network must be mapped to one of the IPAM's networks on the IPAM's
    Networks tab, and the IPAM orchestration action must be enabled, which
    this tool does when it is off;
  - manual: the requester types the IP on each order (sc_nic_0_ip).
- Both: the requester chooses DHCP or a typed static IP per order.

Called with only onb_network it reports the current settings. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import ipaddress
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
MAX_POOL = 1024
EXPECTED = {"dhcp": "dhcp", "pool": "pool", "ipam": "ipam", "manual": "user_static", "both": "user_static_or_dhcp"}


def _text(value):
    return html.unescape(value or "").strip()


def _failure(message, outputs=None):
    out = {"error": message}
    if outputs:
        out.update(outputs)
    return {"status": "FAILURE", "output_message": "", "error_message": message, "outputs": out}


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
        "onb_network": """{{ onb_network }}""",
        "onb_addressing": """{{ onb_addressing }}""",
        "onb_gateway": """{{ onb_gateway }}""",
        "onb_netmask": """{{ onb_netmask }}""",
        "onb_dns1": """{{ onb_dns1 }}""",
        "onb_dns2": """{{ onb_dns2 }}""",
        "onb_dns_domain": """{{ onb_dns_domain }}""",
        "onb_ip_source": """{{ onb_ip_source }}""",
        "onb_pool_range": """{{ onb_pool_range }}""",
        "onb_ipam": """{{ onb_ipam }}""",
    }
    return {key[4:]: _text(value) for key, value in raw.items()}


def _resolve_network(ref):
    from resourcehandlers.models import ResourceNetwork

    net = None
    if GLOBAL_ID_RE.match(ref):
        net = ResourceNetwork.objects.filter(global_id=ref).first()
    elif ref.isdigit():
        net = ResourceNetwork.objects.filter(id=int(ref)).first()
    if net is None:
        matches = list(ResourceNetwork.objects.filter(name__iexact=ref)) or list(ResourceNetwork.objects.filter(network__iexact=ref))
        if len(matches) > 1:
            return None, "Several networks match '{}'; pass the NET- global id (onb_environment_networks lists them).".format(ref)
        net = matches[0] if matches else None
    if net is None:
        return None, "No network matches '{}'.".format(ref)
    try:
        net = net.cast()
    except Exception:
        pass
    return net, None


def _ipv4(value, label):
    try:
        return str(ipaddress.IPv4Address(value)), None
    except Exception:
        return None, "{} '{}' is not an IPv4 address.".format(label, value)


def _netmask(value):
    text = value.strip().lstrip("/")
    try:
        if text.isdigit():
            return str(ipaddress.IPv4Network("0.0.0.0/{}".format(int(text))).netmask), None
        mask = ipaddress.IPv4Address(text)
        ipaddress.IPv4Network("0.0.0.0/{}".format(mask))
        return str(mask), None
    except Exception:
        return None, "Netmask '{}' is not valid; use 255.255.255.0 or a prefix such as 24.".format(value)


def _pool_ips(spec, subnet, gateway):
    """Expand '10.0.5.10-10.0.5.60' or a list of addresses; every address must sit in the network."""
    ips = []
    for part in re.split(r"[\s,]+", spec):
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = ipaddress.IPv4Address(start_s.strip()), ipaddress.IPv4Address(end_s.strip())
            if end < start:
                raise ValueError("Range {} ends before it starts.".format(part))
            if int(end) - int(start) + 1 > MAX_POOL:
                raise ValueError("Range {} holds more than {} addresses.".format(part, MAX_POOL))
            ips.extend(str(ipaddress.IPv4Address(i)) for i in range(int(start), int(end) + 1))
        else:
            ips.append(str(ipaddress.IPv4Address(part)))
    seen, out = set(), []
    for ip in ips:
        addr = ipaddress.IPv4Address(ip)
        if subnet is not None and addr not in subnet:
            raise ValueError("{} is outside the network {} (gateway and netmask).".format(ip, subnet))
        if subnet is not None and addr in (subnet.network_address, subnet.broadcast_address):
            continue
        if ip == gateway or ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    if len(out) > MAX_POOL:
        raise ValueError("More than {} addresses.".format(MAX_POOL))
    return out


def _pool_state(net):
    try:
        rp = net.get_resource_pool()
    except Exception:
        rp = None
    if rp is None:
        return None
    values = rp.get_values()
    return {"name": rp.name, "total": values.count(), "free": values.filter(server=None, resource=None).count(),
            "url": "/resourcepools/{}/".format(rp.id)}


def _ipam_endpoints():
    try:
        from ipam.models import IPAM

        return [{"id": i.id, "name": i.name, "technology": str(i.ipam_technology)} for i in IPAM.objects.all().order_by("name")]
    except Exception:
        return []


def _resolve_ipam(ref):
    from ipam.models import IPAM

    ipam = None
    if ref.isdigit():
        ipam = IPAM.objects.filter(id=int(ref)).first()
    if ipam is None:
        matches = list(IPAM.objects.filter(name__iexact=ref))
        if len(matches) > 1:
            return None, "Several IPAM endpoints are named '{}'; pass its numeric id.".format(ref)
        ipam = matches[0] if matches else None
    if ipam is None:
        return None, "No IPAM endpoint matches '{}'.".format(ref)
    return ipam, None


def _state(net, base):
    try:
        provider = net.determine_ip_provider()
    except Exception:
        provider = None
    row = {
        "id": getattr(net, "global_id", None),
        "name": net.name,
        "url": base + "/networks/{}/".format(net.id),
        "addressing": net.addressing_schema,
        "ipProvider": provider,
        "gateway": net.gateway,
        "netmask": net.netmask,
        "dns1": net.dns1,
        "dns2": net.dns2,
        "dnsDomain": net.dns_domain,
    }
    pool = _pool_state(net)
    if pool:
        pool["url"] = base + pool["url"]
        row["ipPool"] = pool
    if net.ipam_network_id:
        row["ipamNetwork"] = str(net.ipam_network)
    return row


def _remove_pool(net):
    from behavior_mapping.models import ResourcePoolMapping

    mappings = list(ResourcePoolMapping.objects.filter(network=net))
    for mapping in mappings:
        pool = mapping.pool
        if pool is not None and pool.get_values().exclude(server=None, resource=None).exists():
            raise ValueError("The network's IP pool '{}' has addresses in use; free them before removing it.".format(pool.name))
    for mapping in mappings:
        pool = mapping.pool
        mapping.delete()
        if pool is not None:
            pool.delete()
    return len(mappings)


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may change network addressing.")

    inp = _read_inputs()
    if not inp["network"]:
        return _failure("onb_network is required: the NET- global id or the network name (onb_environment_networks lists them).")
    net, error = _resolve_network(inp["network"])
    if error:
        return _failure(error)
    base = _portal_url()
    slug = ""
    if getattr(net, "resource_technology", None) is not None:
        slug = net.resource_technology.slug or ""

    addressing = inp["addressing"].lower()
    source = inp["ip_source"].lower()
    if not addressing and not source and not any(inp[k] for k in ("gateway", "netmask", "dns1", "dns2", "dns_domain", "pool_range", "ipam")):
        return _success("Current addressing of '{}'.".format(net.name), {"network": _state(net, base), "ipamEndpoints": _ipam_endpoints()})

    addressing = addressing or net.addressing_schema
    if addressing not in ("dhcp", "static", "both"):
        return _failure("onb_addressing must be dhcp, static or both.")
    if slug == "vcloud_director" and addressing == "both":
        return _failure("vCloud Director turns 'both' into allocation mode NONE; use dhcp or static.")
    if source and source not in ("pool", "ipam", "manual"):
        return _failure("onb_ip_source must be pool, ipam or manual.")
    if addressing != "static" and source in ("pool", "ipam"):
        return _failure("An IP pool or IPAM supplies static addresses; use onb_addressing=static with onb_ip_source={}.".format(source))

    changes = []
    gateway = net.gateway
    netmask = net.netmask
    if inp["gateway"]:
        gateway, error = _ipv4(inp["gateway"], "Gateway")
        if error:
            return _failure(error)
    if inp["netmask"]:
        netmask, error = _netmask(inp["netmask"])
        if error:
            return _failure(error)
    dns = {}
    for key, label in (("dns1", "DNS 1"), ("dns2", "DNS 2")):
        if inp[key]:
            value, error = _ipv4(inp[key], label)
            if error:
                return _failure(error)
            dns[key] = value
    if inp["dns_domain"] and len(inp["dns_domain"]) > 50:
        return _failure("DNS domain is limited to 50 characters.")
    if addressing in ("static", "both") and not (gateway and netmask):
        return _failure("A static network needs a gateway and a netmask (DNS 1, DNS 2 and DNS domain are optional); pass onb_gateway and onb_netmask.")
    subnet = None
    if gateway and netmask:
        subnet = ipaddress.IPv4Network("{}/{}".format(gateway, netmask), strict=False)
    pool_ips = None
    if source == "pool" and inp["pool_range"]:
        try:
            pool_ips = _pool_ips(inp["pool_range"], subnet, gateway)
        except ValueError as exc:
            return _failure(str(exc))
        if not pool_ips:
            return _failure("onb_pool_range gave no usable address.")
    elif source == "pool" and _pool_state(net) is None:
        return _failure("onb_ip_source=pool needs onb_pool_range: a range such as 10.0.5.10-10.0.5.60 or a list of addresses.")
    if source == "ipam" and not inp["ipam"]:
        endpoints = _ipam_endpoints()
        return _failure(
            "onb_ip_source=ipam needs onb_ipam, the IPAM endpoint's name or id. "
            + ("Endpoints: {}.".format(", ".join("{} ({}, id {})".format(e["name"], e["technology"], e["id"]) for e in endpoints))
               if endpoints else "No IPAM endpoint exists: the user adds one at {}/setup/add-ipam/ (credentials are entered there).".format(base)),
            {"ipamEndpoints": endpoints, "addIpamUrl": base + "/setup/add-ipam/"},
        )

    # Write the settings (the same fields as the network's edit page), only after every input is valid.
    for field, value in (("gateway", gateway), ("netmask", netmask)):
        if value and getattr(net, field) != value:
            setattr(net, field, value)
            changes.append("{} {}".format(field, value))
    for key, value in dns.items():
        if getattr(net, key) != value:
            setattr(net, key, value)
            changes.append("{} {}".format(key, value))
    if inp["dns_domain"] and net.dns_domain != inp["dns_domain"]:
        net.dns_domain = inp["dns_domain"]
        changes.append("DNS domain {}".format(inp["dns_domain"]))
    if net.addressing_schema != addressing:
        net.addressing_schema = addressing
        changes.append("addressing {}".format(addressing))
    net.save()

    notes = []
    want = "dhcp" if addressing == "dhcp" else ("both" if addressing == "both" else (source or None))
    try:
        # Leave only the chosen IP source on the network: a pool or an IPAM link outranks the schema.
        if want != "ipam" and net.ipam_network_id:
            net.ipam_network = None
            net.save()
            changes.append("IPAM link removed")
        if want in ("dhcp", "both", "manual", "ipam") and _pool_state(net):
            removed = _remove_pool(net)
            changes.append("IP pool removed ({} mapping)".format(removed))
        if want == "pool":
            pool = _pool_state(net)
            if pool_ips:
                ips = pool_ips
                if pool is None:
                    net.add_ip_addresses(ips)
                    changes.append("IP pool created with {} address(es)".format(len(ips)))
                else:
                    from infrastructure.models import ResourcePoolValueSet

                    rp = net.get_resource_pool()
                    have = set(rp.get_values().values_list("ip", flat=True))
                    new = [ResourcePoolValueSet(resource_pool=rp, ip=ip) for ip in ips if ip not in have]
                    ResourcePoolValueSet.objects.bulk_create(new)
                    changes.append("IP pool extended by {} address(es)".format(len(new)))
            elif pool is None:
                return _failure("onb_ip_source=pool needs onb_pool_range: a range such as 10.0.5.10-10.0.5.60 or a list of addresses.")
        if want == "ipam":
            if not inp["ipam"]:
                endpoints = _ipam_endpoints()
                return _failure(
                    "onb_ip_source=ipam needs onb_ipam, the IPAM endpoint's name or id. "
                    + ("Endpoints: {}.".format(", ".join("{} ({}, id {})".format(e["name"], e["technology"], e["id"]) for e in endpoints))
                       if endpoints else "No IPAM endpoint exists: the user adds one at {}/setup/add-ipam/ (credentials are entered there).".format(base)),
                    {"ipamEndpoints": endpoints, "addIpamUrl": base + "/setup/add-ipam/"},
                )
            ipam, error = _resolve_ipam(inp["ipam"])
            if error:
                return _failure(error, {"ipamEndpoints": _ipam_endpoints()})
            real = ipam.cast()
            try:
                real.verify_connection()
            except Exception as exc:
                return _failure("IPAM '{}' does not answer: {}: {}. Fix it on {}/ipam/{}/ (Edit Credentials) and verify there.".format(
                    ipam.name, type(exc).__name__, str(exc)[:300], base, ipam.id))
            if (ipam.ipam_technology.modulename or "").startswith("ipam.infoblox") and not net.dns_domain:
                return _failure("Infoblox creates a host record under the network's DNS domain; pass onb_dns_domain.")
            try:
                hook = real.get_hook_module()
            except Exception as exc:
                return _failure("IPAM '{}' has no orchestration action to allocate addresses: {}.".format(ipam.name, str(exc)[:200]))
            if not hook.enabled:
                hook.enabled = True
                hook.save()
                changes.append("enabled the IPAM orchestration action '{}' (applies to every {} endpoint)".format(hook.name, ipam.ipam_technology))
            if not net.ipam_network_id or not net.ipam_network.ipam_set.filter(id=ipam.id).exists():
                outputs = {
                    "network": _state(net, base),
                    "needsUiStep": True,
                    "ipamUrl": base + "/ipam/{}/#tab-networks".format(ipam.id),
                    "next": ("Map the network in the IPAM, then call this tool again with the same inputs to verify: open the "
                             "link, Networks tab, Import IPAM Networks, tick the network that matches {} ({}), then the pencil "
                             "on that row and select CloudBolt network '{}'.".format(subnet or net.name, net.name, net.name)),
                    "changes": changes,
                }
                return _success("'{}' is not mapped to a network of IPAM '{}' yet.".format(net.name, ipam.name), outputs)
    except ValueError as exc:
        return _failure(str(exc), {"network": _state(net, base), "changes": changes})

    # Prove it with the same call the order form and provisioning use.
    state = _state(net, base)
    expected = EXPECTED.get(want) if want else None
    problems = []
    if expected and state["ipProvider"] != expected:
        problems.append("CloudBolt resolves the IP source as '{}' instead of '{}'.".format(state["ipProvider"], expected))
    if state["ipProvider"] in ("user_static", "user_static_or_dhcp", "pool", "ipam") and not (net.gateway and net.netmask):
        problems.append("Static addressing without gateway and netmask fails at provisioning.")
    if state.get("ipPool") and state["ipPool"]["free"] == 0:
        problems.append("The IP pool has no free address.")
    if addressing == "static" and not source and not state.get("ipPool") and not net.ipam_network_id:
        notes.append("Static with no pool or IPAM: the requester types the IP (sc_nic_0_ip) on each order.")
    if state["ipProvider"] in ("user_static", "user_static_or_dhcp"):
        notes.append("Orders on this network must pass sc_nic_0_ip (the static IP).")
    outputs = {"network": state, "changes": changes, "verified": not problems}
    if notes:
        outputs["notes"] = notes
    if problems:
        outputs["problems"] = problems
        return {"status": "FAILURE", "output_message": "", "error_message": " ".join(problems), "outputs": outputs}
    return _success("'{}': IP source {}; {} change(s); verified.".format(net.name, state["ipProvider"], len(changes)), outputs)
