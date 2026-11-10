"""
Onboarding: Failure Trace (MCP Tool Action plugin).

Given an order (ORD-) or job (JOB-) global id, returns every failed job
under it with its errors, output, the tail of its progress messages, its
failed children and a hint for the provisioning failures a new appliance
hits most (no image in the region, no subnet, missing key pair or its
private key, Windows password, guest credentials, unreachable ESXi hosts,
datastore not pinned).
Some failures leave the job's errors field empty and write the exception
only to the job's log file, so for a failed job with no errors the hints are
also matched against the error lines in the tail of that file. The built-in
fetch_order and fetch_job tools stop at the top-level status; this tool
walks down to the job that actually failed and tells the agent which job id
to pass to fetch_job_log for the full traceback. Read-only, synchronous.
Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import json
import os
import re

PROGRESS_TAIL = 15
MAX_DEPTH = 6
MAX_FAILED = 25
LOG_TAIL_BYTES = 256 * 1024
LOG_LEVEL_RE = re.compile(r"\b(ERROR|CRITICAL)\b")
EXCEPTION_LINE_RE = re.compile(r"^\s*[\w.]*(Error|Exception)\b.*:")

HINTS = [
    (re.compile(r"server's password is unknown", re.I), "CloudBolt has no guest password for this server because the vCenter template has no credentials. Set them on the template: onb_ui_link handler_images:<RH id> opens the handler's Templates tab; in the template's Credentials column click + (Add credentials to template), or the pencil when some are set, enter the guest username and password, and Save. Later orders copy them onto the server."),
    (re.compile(r"No password parameter set|cannot set the Administrator password", re.I), "vCenter customization of a Windows server had no Administrator password. Set the Windows template's credentials: onb_ui_link handler_images:<RH id> opens the handler's Templates tab; in the template's Credentials column click + (Add credentials to template) or the pencil, enter Administrator and its password, and Save. Orders copy them onto the server and customization uses that password. Alternative: new_password on the environment (onb_trim_environment reports newPasswordSet and the steps), equal to the template's password unless the customer wants it changed at build."),
    (re.compile(r"guestFile[\s\S]{0,800}?(ConnectTimeout|NewConnectionError|timed out|Connection refused|No route to host|unreachable)|failed to make the HTTP PUT request to the VMware API", re.I), "The appliance could not upload the script to the VM's ESXi host on port 443: VMware Tools file transfer goes straight to the host, not through vCenter, which hosted or provider-managed vCenter often blocks. Set tech_specific_script_execution (Use Tech-Specific Script Execution) to False on the environment: onb_parameter onb_param_name=tech_specific_script_execution, onb_param_targets=<ENV id>, onb_param_options=False. Scripts then run over SSH (22) and WinRM (5985/5986), which must be open from the appliance to the guests. Do not pass the parameter in orders: the order form supplies it and an explicit value is refused. Opening 443 from the appliance to every ESXi host also works."),
    (re.compile(r"Could not find required key material|no matching key material uploaded|key pair has not been added", re.I), "CloudBolt holds no private key for the key pair named in the error. The admin uploads it: onb_ui_link handler_ssh_keys:<RH id> opens the handler's SSH Keys tab; click the pencil in the Actions column of the key's row, paste the private key into Key material, and Save. The key is pasted there, never in chat."),
    (re.compile(r"is required for Azure windows servers|password_arm", re.I), "Azure Windows builds need the password_arm parameter; add it to the environment or order form."),
    (re.compile(r"vmware_datastore parameter must be specified", re.I), "Pin a vmware_datastore value on the vCenter environment's VMware Parameters tab."),
    (re.compile(r"Memory and CPU are required", re.I), "The vCenter environment needs cpu_cnt and mem_size options."),
    (re.compile(r"InvalidAMIID|image.*not found|No (image|template|AMI)|not available in (this )?(region|location)", re.I), "The OS build has no image for this environment's region or location; import one on the handler's images page and attach the OS build to the environment."),
    (re.compile(r"subnet|sc_nic_0|No network", re.I), "No network or subnet is attached to the environment; add one on the environment's Networks tab (sc_nic_0)."),
    (re.compile(r"InvalidKeyPair|key pair(?! has not been added)|key_name", re.I), "The key pair named in key_name does not exist in this region; pin an existing one on the environment."),
    (re.compile(r"InsufficientInstanceCapacity|QuotaExceeded|exceeds quota|OperationNotAllowed.*quota", re.I), "The cloud account's quota or capacity blocked the build; pick a smaller size or another region."),
    (re.compile(r"UnauthorizedOperation|AuthorizationFailed|AccessDenied|Permission to perform this operation was denied", re.I), "The handler's credentials lack a permission for this step; compare with CloudBolt's minimum permissions document for the technology."),
    (re.compile(r"CERTIFICATE_VERIFY_FAILED|SSLError", re.I), "TLS verification failed; add the CA under Admin > SSL Certificates or disable verification for the POC."),
    (re.compile(r"timed out|Connection refused|EndpointConnectionError|Max retries exceeded", re.I), "The appliance could not reach the endpoint; check firewall rules and the web proxy."),
    (re.compile(r"TemplateSyntaxError|VariableDoesNotExist", re.I), "A plugin script has a template token outside a declared input (AGENTS.md cardinal rule 3)."),
    (re.compile(r"ModuleNotFoundError|ImportError", re.I), "A plugin imports a module that is not on the appliance; check shared modules and installed packages."),
    (re.compile(r"hostname.*(exists|in use|duplicate)|Duplicate hostname", re.I), "The generated hostname collides; adjust the hostname template or sequence."),
    (re.compile(r"Remote script|WinRM|SSH.*(refused|timed out|authentication)", re.I), "Guest connectivity failed; check credentials on the OS build or server, and that port 22 or 5985/5986 is reachable from the appliance."),
]


def _text(value):
    return html.unescape(value or "").strip()


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


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
    raw = {"onb_target_id": """{{ onb_target_id }}"""}
    return {"target": _text(raw["onb_target_id"])}


def _iso(value):
    return value.isoformat() if value else None


def _hints_for(text):
    hints = []
    for pattern, hint in HINTS:
        if pattern.search(text) and hint not in hints:
            hints.append(hint)
    return hints


def _progress_tail(job):
    from jobs.models import ProgressMessage

    rows = list(ProgressMessage.objects.filter(job=job).order_by("-id")[:PROGRESS_TAIL])
    rows.reverse()
    return [
        {"time": _iso(row.date), "message": (row.message or "")[:500], "detail": (row.detailed_message or "")[:1500]}
        for row in rows
    ]


def _log_error_text(job):
    """ERROR and CRITICAL entries and exception lines from the last LOG_TAIL_BYTES of the job's log file."""
    try:
        path = job.get_log_file_path()
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > LOG_TAIL_BYTES:
                handle.seek(size - LOG_TAIL_BYTES)
            data = handle.read(LOG_TAIL_BYTES)
    except (OSError, AttributeError, ValueError):
        return ""
    keep = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            # CloudBolt 2026.2+ writes one JSON object per line, with the traceback in exc_info.
            try:
                entry = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(entry, dict) and str(entry.get("levelname") or "").upper() in ("ERROR", "CRITICAL"):
                keep.append("{}\n{}".format(entry.get("message") or "", entry.get("exc_info") or entry.get("exc_text") or ""))
        elif LOG_LEVEL_RE.search(line) or EXCEPTION_LINE_RE.match(line):
            keep.append(line)
    return "\n".join(keep)


def _summary(job, base, with_progress=True):
    text = "{}\n{}".format(job.errors or "", job.output or "")
    log_checked = job.status == "FAILURE" and not (job.errors or "").strip()
    if log_checked:
        text += "\n" + _log_error_text(job)
    row = {
        "id": job.global_id,
        "pk": job.id,
        "type": job.type,
        "label": job.label or "",
        "status": job.status,
        "startDate": _iso(job.start_date),
        "endDate": _iso(job.end_date),
        "errors": (job.errors or "")[:4000],
        "output": (job.output or "")[:2000],
        "url": base + "/jobs/{}/".format(job.id),
        "hints": _hints_for(text),
    }
    if log_checked:
        row["logTailChecked"] = True
    if with_progress:
        row["progressTail"] = _progress_tail(job)
    return row


def _failed_descendants(job, base, depth=0, seen=None):
    seen = seen if seen is not None else set()
    rows = []
    if depth >= MAX_DEPTH:
        return rows
    for child in job.children_jobs.all().order_by("id"):
        if child.id in seen:
            continue
        seen.add(child.id)
        if child.status == "FAILURE":
            rows.append(_summary(child, base, with_progress=False))
        rows.extend(_failed_descendants(child, base, depth + 1, seen))
        if len(rows) >= MAX_FAILED:
            break
    return rows[:MAX_FAILED]


def _failed_servers(job_ids, base):
    """Servers the traced jobs left in Provision Failed state (re-running a failed job in the UI deletes its own)."""
    from infrastructure.models import Server

    servers = Server.objects.filter(jobs__id__in=job_ids, status="PROVFAILED").distinct().order_by("id")
    return [
        {
            "id": s.global_id,
            "hostname": s.hostname,
            "environment": s.environment.name if s.environment else None,
            "inCloud": bool(s.resource_handler_svr_id),
            "url": base + "/servers/{}/".format(s.id),
        }
        for s in servers[:MAX_FAILED]
    ]


def _trace_order(order, base):
    try:
        jobs = list(order.list_of_jobs(recursive=True) or [])
    except TypeError:
        jobs = list(order.list_of_jobs() or [])
    failed = [_summary(j, base) for j in sorted(jobs, key=lambda j: j.id) if j.status == "FAILURE"][:MAX_FAILED]
    outputs = {
        "target": {
            "id": order.global_id,
            "pk": order.id,
            "kind": "order",
            "status": order.status,
            "deniedReason": getattr(order, "denied_reason", None) or None,
            "url": base + "/orders/{}/".format(order.id),
        },
        "totalJobs": len(jobs),
        "failedJobs": failed,
        "failedServers": _failed_servers([j.id for j in jobs], base),
    }
    if order.status == "PENDING":
        outputs["note"] = "The order is awaiting approval; approve it with approve_order (the caller must be an approver) and poll again."
    elif order.status == "DENIED":
        outputs["note"] = "The order was denied; see deniedReason."
    elif order.status in ("ACTIVE", "RUNNING") and not failed:
        outputs["note"] = "The order is still running; nothing has failed yet."
    elif order.status == "SUCCESS" and not failed:
        outputs["note"] = "The order succeeded and no job under it failed."
    return outputs


def _trace_job(job, base):
    outputs = {
        "target": dict(_summary(job, base), kind="job"),
        "failedJobs": _failed_descendants(job, base),
    }
    outputs["failedServers"] = _failed_servers([job.id] + [row["pk"] for row in outputs["failedJobs"]], base)
    if job.status != "FAILURE" and not outputs["failedJobs"]:
        outputs["note"] = "Neither this job nor any job under it has failed (status {}).".format(job.status)
    return outputs


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may read failure traces.")

    target = _read_inputs()["target"]
    if not target:
        return _failure("onb_target_id is required: an order id such as ORD-abc12345 or a job id such as JOB-abc12345.")
    base = _portal_url()
    prefix = target.split("-", 1)[0].upper()

    if prefix == "ORD":
        from orders.models import Order

        order = Order.objects.filter(global_id=target).first()
        if order is None:
            return _failure("Order '{}' not found.".format(target))
        outputs = _trace_order(order, base)
    elif prefix == "JOB":
        from jobs.models import Job

        found = Job.objects.filter(global_id=target).first()
        if found is None:
            return _failure("Job '{}' not found.".format(target))
        outputs = _trace_job(found, base)
    else:
        return _failure("onb_target_id must start with ORD- or JOB-, got '{}'.".format(target))

    failed_ids = [row["id"] for row in outputs["failedJobs"]]
    if outputs["target"].get("kind") == "job" and outputs["target"].get("status") == "FAILURE":
        failed_ids.insert(0, outputs["target"]["id"])
    all_hints = []
    for row in outputs["failedJobs"] + [outputs["target"]]:
        for hint in row.get("hints") or []:
            if hint not in all_hints:
                all_hints.append(hint)
    outputs["hints"] = all_hints
    if failed_ids:
        outputs["next"] = (
            "For the traceback, call fetch_job_log with log_job_id={} and log_file_tail=200; for appliance-wide "
            "context call onb_recent_errors with onb_log=jobengine.".format(failed_ids[0])
        )
    if outputs["failedServers"]:
        outputs["next"] = (outputs.get("next", "") + " " + (
            "{} server(s) were left in Provision Failed state ({}). Once the cause is explained, offer the user to delete "
            "them with onb_failed_servers before ordering again; inCloud true means a VM exists in the cloud."
        ).format(len(outputs["failedServers"]), ", ".join(s["hostname"] or s["id"] for s in outputs["failedServers"]))).strip()
    message = "{}: {} failed job(s).".format(target, len(failed_ids))
    return _success(message, outputs)
