"""
POC Onboarding: Failed Servers (MCP Tool Action plugin).

Lists the servers left in Provision Failed state by orders that did not
clean up after themselves (fetch_server_list only shows active servers, so
the agent cannot see them otherwise), and deletes the ones the user agreed
to. A delete goes through CloudBolt's own decommission (the same call as the
server's Delete button and the v3 API): the delete job removes the VM from
the cloud when the failed build got that far, then retires the record, and
the fast-track decommission preference decides whether it runs as a job or
as an order that may need approval. Only Provision Failed servers are
accepted. Synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

MAX_LISTED = 50
MAX_DELETE = 20
SERVER_ID_RE = re.compile(r"^SVR-[A-Za-z0-9]{6,16}$")


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


def _split(value):
    return [part for part in re.split(r"[\s,]+", _text(value)) if part]


def _read_inputs():
    raw = {"onb_delete_servers": """{{ onb_delete_servers }}"""}
    return {"delete": _split(raw["onb_delete_servers"])}


def _iso(value):
    return value.isoformat() if value else None


def _locked(server):
    """The server_lock parameter (a list such as ["deletion"]) blocks the delete job."""
    lock = getattr(server, "server_lock", None)
    return bool(lock and "deletion" in lock)


def _row(server, base):
    """One failed server with where it came from and what a delete will do."""
    prov = server.jobs.filter(type="provision").order_by("-id").first()
    order = prov.get_order() if prov else None
    return {
        "id": server.global_id,
        "hostname": server.hostname,
        "environment": server.environment.name if server.environment else None,
        "environmentId": server.environment.global_id if server.environment else None,
        "group": server.group.name if server.group else None,
        "owner": server.owner.user.username if server.owner and server.owner.user else None,
        "created": _iso(server.add_date),
        "provisionJob": prov.global_id if prov else None,
        "order": order.global_id if order else None,
        "inCloud": bool(server.resource_handler_svr_id),
        "locked": _locked(server),
        "url": base + "/servers/{}/".format(server.id),
    }


def _list(base):
    from infrastructure.models import Server

    failed = Server.objects.filter(status="PROVFAILED").select_related("environment", "group", "owner__user")
    rows = [_row(s, base) for s in failed.order_by("-add_date")[:MAX_LISTED]]
    total = failed.count()
    outputs = {"failedServerCount": total, "failedServers": rows}
    if total > MAX_LISTED:
        outputs["note"] = "Showing the newest {} of {}.".format(MAX_LISTED, total)
    if total:
        outputs["next"] = (
            "Show the user each server (hostname, environment, created, inCloud) and ask whether to delete them; "
            "inCloud true means a VM was created and the delete removes it from the cloud too. Call again with "
            "onb_delete_servers set to the SVR- ids the user agreed to."
        )
    return outputs


def _delete(ids, profile, base):
    from infrastructure.models import Server
    from infrastructure.services import ServerService

    deleted, skipped = [], []
    for ref in ids:
        if not SERVER_ID_RE.match(ref):
            skipped.append({"id": ref, "reason": "Not a server global id (SVR-...)."})
            continue
        server = Server.objects.filter(global_id=ref).first()
        if server is None:
            skipped.append({"id": ref, "reason": "No server has this id."})
            continue
        if server.status != "PROVFAILED":
            skipped.append({
                "id": ref,
                "hostname": server.hostname,
                "reason": "Status is {}, not Provision Failed; this tool deletes only failed builds.".format(server.status),
            })
            continue
        if _locked(server):
            skipped.append({"id": ref, "hostname": server.hostname, "reason": "Locked against deletion on the server page."})
            continue
        row = _row(server, base)
        try:
            job, order = ServerService(server).decommission(profile)
        except Exception as error:
            skipped.append({"id": ref, "hostname": server.hostname, "reason": "{}: {}".format(type(error).__name__, error)})
            continue
        row["deleteJob"] = job.global_id if job else None
        row["deleteOrder"] = order.global_id if order else None
        if order is not None:
            row["deleteOrderStatus"] = order.status
        deleted.append(row)
    return deleted, skipped


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may list or delete failed servers.")

    base = _portal_url()
    ids = _read_inputs()["delete"]
    if not ids:
        outputs = _list(base)
        return _success("{} server(s) in Provision Failed state.".format(outputs["failedServerCount"]), outputs)

    if len(ids) > MAX_DELETE:
        return _failure("At most {} servers per call; {} were given.".format(MAX_DELETE, len(ids)))
    deleted, skipped = _delete(ids, profile, base)
    outputs = {"deleted": deleted, "skipped": skipped}
    if deleted:
        outputs["next"] = (
            "Poll each deleteJob with fetch_job until it finishes (a deleteOrder in PENDING needs approve_order first). "
            "Then call this tool with no input: the deleted servers are gone from the list. A failed delete job: "
            "onb_failure_trace with its id; when the cloud refused the delete the server comes back as active."
        )
    message = "Delete started for {} server(s), {} skipped.".format(len(deleted), len(skipped))
    if not deleted and skipped:
        return _failure(message + " " + " ".join("{}: {}".format(s["id"], s["reason"]) for s in skipped))
    return _success(message, outputs)
