"""
POC Onboarding: Scorecard (MCP Tool Action plugin).

Builds the POC Test Plan scorecard: every item with its weight, the status
recorded with onb_set_item_status, and the live evidence on the appliance
(orders, handlers, environments, parameters, mappings, roles, approvals,
quotas, lifecycle parameters, recurring jobs, rules, CIT tests, webhooks,
extensions). A recorded status wins; evidence only suggests. Read-only,
synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import json

from django.conf import settings

STATE_FIELD = "poc_onboarding_state"
SECTIONS = {
    "all": None,
    "self-service": "Self-Service",
    "rbac": "Role-Based Access Control and Governance",
    "resource-management": "Resource Management",
    "extensibility": "Extensibility",
}
CATEGORIES = [
    ("Self-Service", [
        ("1.1.1", "Provision a standardized blueprint from the self-service catalog", 5),
        ("1.1.2", "User-defined parameters during provisioning", 5),
        ("1.1.3", "Post-provisioning automation from a catalog blueprint", 5),
        ("1.1.4", "Hostname naming convention with preview", 5),
        ("1.2.1", "One blueprint deploys to two clouds", 5),
        ("1.2.2", "Cross-cloud tagging policy", 5),
        ("1.2.3", "Unified visibility of servers across clouds", 5),
    ]),
    ("Role-Based Access Control and Governance", [
        ("2.1.1", "LDAP or Active Directory authentication", 3),
        ("2.1.2", "Group and role assignment from AD group membership", 3),
        ("2.1.3", "SAML2 single sign-on", 2),
        ("2.2.1", "Per-tenant branded portal", 3),
        ("2.3.1", "Resource visibility constrained by group role", 4),
        ("2.3.2", "Blueprint entitlement by group", 4),
        ("2.3.3", "Environment entitlement by group", 4),
        ("2.3.4", "Custom role", 3),
        ("2.4.1", "Basic order approval", 3),
        ("2.4.2", "Approval on a day-2 action", 3),
        ("2.5.1", "Tagging policy from a parameter", 5),
        ("2.5.2", "Group quota", 2),
    ]),
    ("Resource Management", [
        ("3.1.1", "Power schedules", 5),
        ("3.1.2", "Leases (expiration dates)", 5),
        ("3.1.3", "Self-service decommission", 4),
        ("3.1.4", "Onboard existing cloud resources", 5),
        ("3.2.1", "Resize a VM", 4),
        ("3.2.2", "Configuration update through Ansible", 0),
        ("3.2.3", "Lifecycle actions (start, stop, reboot)", 3),
    ]),
    ("Extensibility", [
        ("4.1.1", "Agentless", 5),
        ("4.1.2", "Agentless in-guest script execution", 5),
        ("4.2.1", "UI extension from the Content Library", 5),
        ("4.2.2", "Recurring job on a custom schedule", 4),
        ("4.2.3", "Rule-based automation", 3),
        ("4.2.4", "Continuous infrastructure testing", 3),
        ("4.2.5", "Inbound webhook", 4),
        ("4.2.6", "Customizable notification templates", 0),
        ("4.3.1", "Python generated options for a parameter", 5),
        ("4.3.2", "PowerShell in-guest script as a build item", 4),
        ("4.3.3", "Bash in-guest script as a build item", 4),
        ("4.3.4", "Terraform-backed blueprint", 0),
        ("4.3.5", "Ansible Automation Platform configuration", 0),
    ]),
]
PREREQS = [
    ("P1", "CMP license key installed"), ("P2", "Appliance deployed, initial configuration done"),
    ("P3", "Up to two resource handlers, one or two environments each"), ("P4", "Custom Server deployed in each environment"),
    ("P5", "Outbound 443 to content.cloudbolt.app"), ("P6", "Web proxy configured, if required"), ("P7", "SMTP configured"),
    ("P8", "DNS A record for the portal"), ("P9", "SSO application and SME"), ("P10", "AD/LDAP service account and group DN"),
    ("P11", "Ansible or AAP prerequisites"), ("P12", "Terraform scripts in git"),
]
BUILTIN_TAG_ATTRIBUTES = {"environment", "group", "hostname", "os_build", "os_family", "owner"}


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


def _read_inputs():
    raw = {"onb_score_section": """{{ onb_score_section }}"""}
    return {"section": (_text(raw["onb_score_section"]) or "all").lower()}


def _load_state():
    try:
        from infrastructure.models import CustomField
        from orders.models import CustomFieldValue

        field = CustomField.objects.filter(name=STATE_FIELD).first()
        if field is None:
            return {}
        cfv = CustomFieldValue.objects.filter(field=field).order_by("id").first()
        if cfv is None:
            return {}
        data = json.loads(cfv.txt_value or "")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe(func):
    try:
        return func()
    except Exception as exc:
        return {"found": None, "detail": "could not evaluate: {}: {}".format(type(exc).__name__, str(exc)[:160])}


def _ev(found, detail):
    return {"found": bool(found), "detail": detail}


# --- evidence per item -----------------------------------------------------

def _ev_1_1_1():
    from orders.models import BlueprintOrderItem

    name = getattr(settings, "DEFAULT_NEW_SERVER_BLUEPRINT_NAME", "Custom Server")
    custom = BlueprintOrderItem.objects.filter(blueprint__name=name, order__status="SUCCESS").values("order").distinct().count()
    any_bp = BlueprintOrderItem.objects.filter(order__status="SUCCESS").values("order").distinct().count()
    return _ev(custom or any_bp, "{} successful Custom Server order(s), {} successful blueprint order(s) in all".format(custom, any_bp))


def _ev_1_1_2():
    from accounts.models import Group
    from infrastructure.models import Environment
    from resourcehandlers.models import ResourceHandler

    tech = set()
    for rh in ResourceHandler.objects.all():
        try:
            rh = rh.cast()
            tech |= set(rh.tech_parameter_fields() or []) | set(rh.special_fields() or []) | set(getattr(rh, "base_orders_cf_names", []) or [])
        except Exception:
            pass
    names = set()
    for env in Environment.objects.all():
        names |= {n for n in env.custom_fields.values_list("name", flat=True) if n not in tech and not n.startswith("sc_nic_")}
    for group in Group.objects.all():
        names |= set(group.custom_fields.values_list("name", flat=True))
    names -= {"hostname_template", "expiration_date", "power_schedule", "annotation", "poc_onboarding_state"}
    return _ev(names, "parameters on environments or groups: {}".format(", ".join(sorted(names)[:12]) or "none"))


def _ev_1_1_3():
    from servicecatalog.models import RunRemoteScriptHookServiceItem, ServiceBlueprint

    scripts = RunRemoteScriptHookServiceItem.objects.filter(blueprint__status="ACTIVE").count()
    bps = ServiceBlueprint.objects.filter(status="ACTIVE", name="POC Server").count()
    return _ev(scripts, "{} remote-script build item(s) on active blueprints; POC Server blueprint present: {}".format(scripts, bool(bps)))


def _ev_1_1_4():
    from infrastructure.models import Environment
    from servicecatalog.models import ProvisionServerServiceItem

    envs = Environment.objects.filter(custom_field_options__field__name="hostname_template").distinct().count()
    tiers = ProvisionServerServiceItem.objects.exclude(hostname_template="").exclude(hostname_template__isnull=True).count()
    return _ev(envs or tiers, "{} environment(s) and {} server tier(s) carry a hostname template".format(envs, tiers))


def _server_tech_counts():
    from infrastructure.models import Server

    rows = Server.objects.filter(status="ACTIVE").exclude(resource_handler__isnull=True).values_list("resource_handler__resource_technology__name", flat=True)
    counts = {}
    for name in rows:
        counts[name] = counts.get(name, 0) + 1
    return counts


def _ev_1_2_1():
    counts = _server_tech_counts()
    return _ev(len(counts) >= 2, "active servers per technology: {}".format(", ".join("{} ({})".format(k, v) for k, v in sorted(counts.items())) or "none"))


def _ev_1_2_2():
    from tags.models import TaggableAttribute

    per_handler = {}
    for ta in TaggableAttribute.objects.exclude(resource_handler__isnull=True):
        per_handler.setdefault(ta.resource_handler.name, set()).add(ta.attribute)
    common = set.intersection(*per_handler.values()) if len(per_handler) >= 2 else set()
    return _ev(len(per_handler) >= 2 and common, "handlers with tag mappings: {}; attributes mapped on all of them: {}".format(
        ", ".join(sorted(per_handler)) or "none", ", ".join(sorted(common)) or "none"))


def _ev_1_2_3():
    counts = _server_tech_counts()
    return _ev(len(counts) >= 2, "servers visible from {} technologies".format(len(counts)))


def _ev_2_1_1():
    from utilities.models import LDAPUtility

    utilities = list(LDAPUtility.objects.all())
    ldap_users = None
    try:
        from accounts.models import UserProfile

        ldap_users = UserProfile.objects.exclude(ldap__isnull=True).count()
    except Exception:
        pass
    return _ev(utilities, "{} LDAP utility(ies){}".format(len(utilities), "; {} directory user(s) seen".format(ldap_users) if ldap_users is not None else ""))


def _ev_2_1_2():
    from utilities.models import LDAPMapping

    rows = list(LDAPMapping.objects.all()[:10])
    return _ev(rows, "{} LDAP group mapping(s)".format(LDAPMapping.objects.count()))


def _ev_2_1_3():
    from authentication.sso.models import BaseSSOProvider

    count = BaseSSOProvider.objects.count()
    return _ev(count, "{} SSO provider(s)".format(count))


def _ev_2_2_1():
    from portals.models import PortalConfig

    count = PortalConfig.objects.count()
    return _ev(count > 1, "{} portal(s); more than one means a branded portal exists".format(count))


def _ev_2_3_1():
    from accounts.models import GroupRoleMembership

    roles = set(GroupRoleMembership.objects.filter(profile__super_admin=False).values_list("role__name", flat=True))
    return _ev({"requestor", "resource_admin"} & roles, "roles held by non-admin users: {}".format(", ".join(sorted(roles)) or "none"))


def _ev_2_3_2():
    from servicecatalog.models import ServiceBlueprint

    restricted = ServiceBlueprint.objects.filter(status="ACTIVE", any_group_can_deploy=False)
    with_groups = [bp.name for bp in restricted if bp.groups.exists()][:8]
    return _ev(with_groups, "blueprints restricted to groups: {}".format(", ".join(with_groups) or "none"))


def _ev_2_3_3():
    from infrastructure.models import Environment

    rows = [e.name for e in Environment.objects.all() if e.groups_served.exists()][:8]
    return _ev(rows, "environments entitled to specific groups: {}".format(", ".join(rows) or "none"))


def _ev_2_3_4():
    from accounts.models import Role

    seed = set()
    try:
        from initialize.cb_minimal import all_grouptype_roles, all_roles

        seed = {r["name"] for r in list(all_roles) + list(all_grouptype_roles)}
    except Exception:
        seed = {"requestor", "approver", "viewer", "resource_admin", "group_admin", "job_owner", "reassigner", "resource_viewer"}
    custom = sorted(Role.objects.exclude(name__in=seed).values_list("name", flat=True))
    return _ev(custom, "custom roles: {}".format(", ".join(custom) or "none"))


def _ev_2_4_1():
    from django.db.models import F
    from orders.models import Order

    approved = Order.objects.filter(status__in=["SUCCESS", "ACTIVE", "FAILURE"]).exclude(approved_by__isnull=True).exclude(approved_by=F("owner")).count()
    pending = Order.objects.filter(status="PENDING").count()
    return _ev(approved, "{} order(s) approved by someone other than the requester; {} pending now".format(approved, pending))


def _ev_2_4_2():
    from cbhooks.models import ResourceAction, ServerAction

    labels = list(ServerAction.objects.filter(requires_approval=True).values_list("label", flat=True)) + list(
        ResourceAction.objects.filter(requires_approval=True).values_list("label", flat=True))
    return _ev(labels, "actions requiring approval: {}".format(", ".join(labels[:8]) or "none"))


def _ev_2_5_1():
    from tags.models import TaggableAttribute

    params = sorted({ta.attribute for ta in TaggableAttribute.objects.all() if ta.attribute not in BUILTIN_TAG_ATTRIBUTES})
    return _ev(params, "parameters mapped to cloud tags: {}".format(", ".join(params) or "none"))


def _ev_2_5_2():
    from accounts.models import Group

    rows = []
    for group in Group.objects.exclude(name="Unassigned"):
        try:
            item = group.quota_set.vm_cnt
            if item is not None and item.limit is not None and item.limit != float("inf"):
                rows.append("{} ({})".format(group.name, int(item.limit)))
        except Exception:
            pass
    return _ev(rows, "groups with a server quota: {}".format(", ".join(rows[:8]) or "none"))


def _envs_with(name):
    from infrastructure.models import Environment

    return [e.name for e in Environment.objects.filter(custom_fields__name=name).distinct()][:8]


def _ev_3_1_1():
    envs = _envs_with("power_schedule")
    return _ev(envs, "environments offering power_schedule: {}".format(", ".join(envs) or "none"))


def _ev_3_1_2():
    from jobs.models import RecurringJob

    envs = _envs_with("expiration_date")
    expire = RecurringJob.objects.filter(name__icontains="expire", enabled=True).exists()
    return _ev(envs and expire, "environments offering expiration_date: {}; Expire Servers job enabled: {}".format(", ".join(envs) or "none", expire))


def _ev_3_1_3():
    from jobs.models import Job

    count = Job.objects.filter(type="decom", status="SUCCESS").count()
    return _ev(count, "{} successful decommission job(s)".format(count))


def _ev_3_1_4():
    from infrastructure.models import Server

    unassigned = Server.objects.filter(status="ACTIVE", group__name="Unassigned").count()
    return _ev(None, "{} active server(s) still in Unassigned; onboarding is recorded manually".format(unassigned))


def _ev_3_2_1():
    from jobs.models import Job

    count = Job.objects.filter(status="SUCCESS", job_parameters__hookparameters__hook__name__icontains="resize").count()
    return _ev(count, "{} successful Resize job(s)".format(count))


def _ev_3_2_2():
    try:
        from connectors.models import ConnectorConf

        count = ConnectorConf.objects.count()
    except Exception:
        count = 0
    return _ev(count, "{} configuration manager(s) configured".format(count))


def _ev_3_2_3():
    # Power operations run inline and are recorded in server history, not as jobs.
    from history.models import ServerHistory

    count = 0
    for field in ("event_message", "message", "description"):
        try:
            count = ServerHistory.objects.filter(**{field + "__iregex": r"power(ed)? (on|off)|reboot"}).count()
            break
        except Exception:
            continue
    return _ev(count, "{} power on/off/reboot event(s) in server history".format(count))


def _ev_4_1_2():
    from cbhooks.models import ServerAction

    labels = list(ServerAction.objects.filter(label__icontains="ad-hoc").values_list("label", flat=True)) + list(
        ServerAction.objects.filter(label__icontains="ad hoc").values_list("label", flat=True))
    return _ev(labels, "ad-hoc script actions: {}".format(", ".join(labels) or "none"))


def _ev_4_2_1():
    from extensions.models import UIExtension

    names = list(UIExtension.objects.values_list("name", flat=True))
    return _ev(names, "UI extensions: {}".format(", ".join(names[:10]) or "none"))


def _ev_4_2_2():
    from jobs.models import RecurringJob

    names = list(RecurringJob.objects.filter(enabled=True).values_list("name", flat=True))
    return _ev(names, "enabled recurring jobs: {}".format(", ".join(names[:10]) or "none"))


def _ev_4_2_3():
    from cbhooks.models import TriggerPoint

    names = list(TriggerPoint.objects.filter(enabled=True).values_list("name", flat=True))
    return _ev(names, "enabled rules: {}".format(", ".join(names[:10]) or "none"))


def _ev_4_2_4():
    from cscv.models import CITTest
    from jobs.models import Job

    tests = CITTest.objects.count()
    runs = Job.objects.filter(type="functionaltest").count()
    return _ev(tests, "{} CIT test(s), {} test run(s)".format(tests, runs))


def _ev_4_2_5():
    from cbhooks.models import InboundWebHook

    labels = list(InboundWebHook.objects.values_list("label", flat=True))
    return _ev(labels, "inbound webhooks: {}".format(", ".join(labels[:10]) or "none"))


def _ev_4_3_1():
    from cbhooks.models import HookPoint

    hp = HookPoint.objects.filter(label__iexact="Generated Parameter Options").first() or HookPoint.objects.filter(name__icontains="generated").first()
    count = 0
    if hp is not None:
        for attr in ("hookpointaction_set", "hookpointactions", "actions"):
            try:
                count = getattr(hp, attr).filter(enabled=True).count()
                break
            except Exception:
                continue
    return _ev(count, "{} enabled generated-options action(s)".format(count))


def _ev_scripts(os_family):
    from cbhooks.models import RemoteScriptHook

    rows = list(RemoteScriptHook.objects.filter(target_os_families__name__icontains=os_family).distinct().values_list("name", flat=True))
    return _ev(rows, "{} remote scripts: {}".format(os_family, ", ".join(rows[:8]) or "none"))


def _ev_4_3_4():
    from servicecatalog.models import ServiceItem

    count = 0
    for item in ServiceItem.objects.filter(blueprint__status="ACTIVE"):
        try:
            if "terraform" in type(item.cast()).__name__.lower():
                count += 1
        except Exception:
            continue
    return _ev(count, "{} Terraform build item(s) on active blueprints".format(count))


EVIDENCE = {
    "1.1.1": _ev_1_1_1, "1.1.2": _ev_1_1_2, "1.1.3": _ev_1_1_3, "1.1.4": _ev_1_1_4, "1.2.1": _ev_1_2_1, "1.2.2": _ev_1_2_2, "1.2.3": _ev_1_2_3,
    "2.1.1": _ev_2_1_1, "2.1.2": _ev_2_1_2, "2.1.3": _ev_2_1_3, "2.2.1": _ev_2_2_1, "2.3.1": _ev_2_3_1, "2.3.2": _ev_2_3_2, "2.3.3": _ev_2_3_3,
    "2.3.4": _ev_2_3_4, "2.4.1": _ev_2_4_1, "2.4.2": _ev_2_4_2, "2.5.1": _ev_2_5_1, "2.5.2": _ev_2_5_2,
    "3.1.1": _ev_3_1_1, "3.1.2": _ev_3_1_2, "3.1.3": _ev_3_1_3, "3.1.4": _ev_3_1_4, "3.2.1": _ev_3_2_1, "3.2.2": _ev_3_2_2, "3.2.3": _ev_3_2_3,
    "4.1.1": lambda: _ev(True, "CloudBolt uses SSH and WinRM; no agent is installed in guests"),
    "4.1.2": _ev_4_1_2, "4.2.1": _ev_4_2_1, "4.2.2": _ev_4_2_2, "4.2.3": _ev_4_2_3, "4.2.4": _ev_4_2_4, "4.2.5": _ev_4_2_5,
    "4.2.6": lambda: _ev(None, "email template edits are not detectable; record manually"),
    "4.3.1": _ev_4_3_1, "4.3.2": lambda: _ev_scripts("Windows"), "4.3.3": lambda: _ev_scripts("Linux"), "4.3.4": _ev_4_3_4, "4.3.5": _ev_3_2_2,
}


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may read the scorecard.")

    section = _read_inputs()["section"]
    if section not in SECTIONS:
        return _failure("onb_score_section must be one of: {}.".format(", ".join(SECTIONS)))
    wanted = SECTIONS[section]
    recorded = (_load_state().get("items") or {})

    categories = []
    totals = {"items": 0, "done": 0, "notApplicable": 0, "pending": 0, "pointsPossible": 0, "pointsPossibleApplicable": 0}
    for name, items in CATEGORIES:
        if wanted and name != wanted:
            continue
        rows = []
        possible = 0
        applicable = 0
        for item_id, title, weight in items:
            record = recorded.get(item_id) or {}
            evidence = _safe(EVIDENCE[item_id]) if item_id in EVIDENCE else {"found": None, "detail": ""}
            status = record.get("status")
            if status in ("skipped", "not_applicable"):
                suggested = "not_applicable"
            elif status == "done":
                suggested = "done"
            elif evidence.get("found"):
                suggested = "done (evidence)"
            else:
                suggested = "pending"
            possible += 5 * weight
            if suggested != "not_applicable":
                applicable += 5 * weight
            totals["items"] += 1
            if suggested.startswith("done"):
                totals["done"] += 1
            elif suggested == "not_applicable":
                totals["notApplicable"] += 1
            else:
                totals["pending"] += 1
            rows.append({
                "id": item_id, "title": title, "weight": weight,
                "recorded": status, "recordedNote": record.get("note") or None,
                "evidence": evidence.get("detail"), "evidenceFound": evidence.get("found"),
                "suggested": suggested,
            })
        totals["pointsPossible"] += possible
        totals["pointsPossibleApplicable"] += applicable
        categories.append({"category": name, "pointsPossible": possible, "pointsPossibleApplicable": applicable, "items": rows})

    prereqs = [{"id": pid, "title": title, "recorded": (recorded.get(pid) or {}).get("status"), "note": (recorded.get(pid) or {}).get("note")} for pid, title in PREREQS]
    outputs = {
        "section": section,
        "categories": categories,
        "prerequisites": prereqs if not wanted else [],
        "totals": totals,
        "scoring": "The workbook multiplies a 0 to 5 score per item by its weight; points possible are 5 times the weight. Items recorded not applicable are excluded from pointsPossibleApplicable.",
        "next": "Read the pending items to the user; record each outcome with onb_set_item_status (done, or not_applicable with a reason), then call this tool again for the final table.",
    }
    message = "{} item(s): {} done, {} not applicable, {} pending.".format(totals["items"], totals["done"], totals["notApplicable"], totals["pending"])
    return _success(message, outputs)
