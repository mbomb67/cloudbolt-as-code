"""
POC Onboarding: Recent Errors (MCP Tool Action plugin).

Reads the tail of a CloudBolt log (application, jobengine, authentication,
mcp, saml2) and returns the WARNING-and-above entries of the last N minutes
grouped by exception and message, newest first, with secrets redacted and a
hint for the patterns a new appliance hits most (failed handler
verification, missing egress, proxy, SSL inspection, LDAP bind). The agent
calls it right after something failed in the UI, since the handler form
masks the real error but logs it. Read-only, synchronous. Admin only.

Only the last 8 MB of the file are read, so the call stays fast on a busy
appliance. CloudBolt 2026.2+ writes one JSON object per line; older plain
text lines are parsed too.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import json
import os
import re
from collections import OrderedDict
from datetime import datetime, timedelta

from django.conf import settings

LOG_FILES = {
    "application": "application.log",
    "jobengine": "jobengine.log",
    "authentication": "authentication.log",
    "mcp": "mcp.log",
    "saml2": "saml2.log",
}
LEVEL_RANKS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
TAIL_BYTES = 8 * 1024 * 1024
DEFAULT_MINUTES = 30
MAX_MINUTES = 7 * 24 * 60
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
SAMPLE_CHARS = 1500
TEXT_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.]\d+\s+\[?(\w+)\]?\s*(.*)$")

REDACTIONS = [
    (re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|client_secret|authentication_key)(\W{0,3}[:=]\W{0,3})(\S+)"), r"\1\2[redacted]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA[redacted]"),
    (re.compile(r"(?i)(aws_secret_access_key\W{0,3}[:=]\W{0,3})\S+"), r"\1[redacted]"),
]

HINTS = [
    (re.compile(r"Failed to validate connection", re.I), "A resource handler form failed Verify Connection; the exception under this line is the real cause. Fix the credentials or permissions, then run onb_verify_resource_handler."),
    (re.compile(r"InvalidClientTokenId|SignatureDoesNotMatch|AuthFailure", re.I), "AWS rejected the access key or secret. Re-enter them on the handler's edit page."),
    (re.compile(r"UnauthorizedOperation|AccessDenied(Exception)?", re.I), "The AWS credentials lack a permission. Compare the IAM policy with CloudBolt's minimum AWS permissions document."),
    (re.compile(r"AADSTS7000215", re.I), "Azure says the client secret is invalid (expired or mistyped). Create a new secret on the app registration and re-enter it."),
    (re.compile(r"AADSTS700016|AADSTS90002", re.I), "Azure cannot find the application or tenant: check the tenant id and client (application) id."),
    (re.compile(r"AuthorizationFailed|does not have authorization to perform action", re.I), "The Azure service principal has no role on the subscription. Assign Reader plus the roles in CloudBolt's Azure permissions document."),
    (re.compile(r"Cannot complete login due to an incorrect user name or password", re.I), "vCenter rejected the username or password. Use user@domain form and re-enter the password."),
    (re.compile(r"CERTIFICATE_VERIFY_FAILED|certificate verify failed|SSLError", re.I), "TLS verification failed. Add the CA to Admin > SSL Certificates, or turn off SSL verification for a POC."),
    (re.compile(r"Name or service not known|nodename nor servname|getaddrinfo failed", re.I), "DNS on the appliance cannot resolve the endpoint hostname."),
    (re.compile(r"Connection refused|timed out|ConnectTimeout|EndpointConnectionError|Max retries exceeded", re.I), "The appliance cannot reach the endpoint on its port: check outbound firewall rules and the web proxy under Admin > Miscellaneous Settings."),
    (re.compile(r"ProxyError|407 Proxy", re.I), "The outbound proxy refused the request; check the proxy host, port and credentials."),
    (re.compile(r"content\.cloudbolt\.app", re.I), "Content Library access failed; the appliance needs HTTPS to content.cloudbolt.app, directly or through the proxy."),
    (re.compile(r"INVALID_CREDENTIALS|ldap.*bind|SERVER_DOWN", re.I), "LDAP bind failed: check the service account DN and password, the port (389 or 636) and the protocol on the LDAP utility."),
    (re.compile(r"is required for Azure windows servers|password_arm", re.I), "Azure Windows builds need the password_arm parameter on the order form or environment."),
    (re.compile(r"vmware_datastore parameter must be specified", re.I), "Pin a vmware_datastore value on the vCenter environment."),
    (re.compile(r"Memory and CPU are required", re.I), "The vCenter environment needs cpu_cnt and mem_size options."),
    (re.compile(r"No (image|template|AMI).*(region|location)|not available in", re.I), "The OS build has no image in this environment's region or location; import one for that region."),
]


def _text(value):
    return html.unescape(value or "").strip()


def _int(value, default, low, high):
    text = _text(value)
    if not text:
        return default
    try:
        number = int(float(text))
    except ValueError:
        return default
    return max(low, min(high, number))


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


def _read_inputs():
    raw = {
        "onb_log": """{{ onb_log }}""",
        "onb_minutes": """{{ onb_minutes }}""",
        "onb_contains": """{{ onb_contains }}""",
        "onb_limit": """{{ onb_limit }}""",
        "onb_min_level": """{{ onb_min_level }}""",
    }
    return {
        "log": (_text(raw["onb_log"]) or "application").lower(),
        "minutes": _int(raw["onb_minutes"], DEFAULT_MINUTES, 0, MAX_MINUTES),
        "contains": _text(raw["onb_contains"]).lower(),
        "limit": _int(raw["onb_limit"], DEFAULT_LIMIT, 1, MAX_LIMIT),
        "min_level": (_text(raw["onb_min_level"]) or "WARNING").upper(),
    }


def _log_dir():
    return os.path.join(getattr(settings, "VARDIR", "/var/"), "log", "cloudbolt")


def _tail_lines(path):
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        if size > TAIL_BYTES:
            handle.seek(size - TAIL_BYTES)
            data = handle.read()
            data = data.split(b"\n", 1)[1] if b"\n" in data else b""
        else:
            data = handle.read()
    return data.decode("utf-8", errors="replace").splitlines(), size


def _parse(line):
    """Return (time, level, logger, module, lineno, message, traceback) or None."""
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            entry = json.loads(stripped)
        except ValueError:
            return None
        if not isinstance(entry, dict):
            return None
        when = _parse_time(entry.get("asctime"))
        message = str(entry.get("message") or "")
        traceback = entry.get("exc_info") or entry.get("exc_text") or entry.get("stack_info") or ""
        return (
            when,
            str(entry.get("levelname") or "").upper(),
            str(entry.get("name") or ""),
            str(entry.get("module") or ""),
            entry.get("lineno"),
            message,
            str(traceback),
        )
    match = TEXT_LINE_RE.match(stripped)
    if match:
        return (_parse_time(match.group(1)), match.group(2).upper(), "", "", None, match.group(3), "")
    return None


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).split(",", 1)[0].split(".", 1)[0], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _redact(text):
    count = 0
    for pattern, replacement in REDACTIONS:
        text, n = pattern.subn(replacement, text)
        count += n
    return text, count


def _group_key(level, logger, message):
    first = message.strip().splitlines()[0] if message.strip() else ""
    normalized = re.sub(r"0x[0-9a-f]+|\b\d+\b|[A-Z]{2,4}-[a-z0-9]{8}", "#", first)[:120]
    return "{}|{}|{}".format(level, logger, normalized)


def _hints_for(text):
    hints = []
    for pattern, hint in HINTS:
        if pattern.search(text) and hint not in hints:
            hints.append(hint)
    return hints


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may read appliance logs.")

    inputs = _read_inputs()
    filename = LOG_FILES.get(inputs["log"])
    if not filename:
        return _failure("Unknown log '{}'. Allowed: {}.".format(inputs["log"], ", ".join(LOG_FILES)))
    if inputs["min_level"] not in LEVEL_RANKS:
        return _failure("Unknown level '{}'. Allowed: {}.".format(inputs["min_level"], ", ".join(LEVEL_RANKS)))
    path = os.path.join(_log_dir(), filename)
    if not os.path.exists(path):
        return _failure("Log file does not exist: {}".format(path))

    try:
        lines, size = _tail_lines(path)
    except OSError as exc:
        return _failure("Could not read {}: {}".format(path, exc))

    # Log timestamps are naive appliance-local time, so compare with naive local now.
    cutoff = datetime.now() - timedelta(minutes=inputs["minutes"]) if inputs["minutes"] else None
    min_rank = LEVEL_RANKS[inputs["min_level"]]
    groups = OrderedDict()
    scanned = matched = redacted = 0
    last_entry = None
    for line in lines:
        parsed = _parse(line)
        if parsed is None:
            # Continuation of a traceback: attach to the previous matching entry.
            if last_entry is not None and line.startswith((" ", "\t", "Traceback")):
                last_entry["traceback"].append(line.rstrip())
            continue
        scanned += 1
        when, level, logger, module, lineno, message, traceback = parsed
        last_entry = None
        rank = LEVEL_RANKS.get(level)
        if rank is None or rank < min_rank:
            continue
        if cutoff is not None and (when is None or when < cutoff):
            continue
        if inputs["contains"] and inputs["contains"] not in (message + " " + traceback).lower():
            continue
        matched += 1
        message, n1 = _redact(message)
        traceback, n2 = _redact(traceback)
        redacted += n1 + n2
        key = _group_key(level, logger, message)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "level": level,
                "logger": logger or None,
                "where": "{}:{}".format(module, lineno) if module else None,
                "count": 0,
                "firstSeen": when.isoformat() if when else None,
                "lastSeen": None,
                "sample": "",
                "traceback": [],
            }
        group["count"] += 1
        group["lastSeen"] = when.isoformat() if when else group["lastSeen"]
        group["sample"] = message[:SAMPLE_CHARS]
        group["traceback"] = traceback.splitlines()[-25:] if traceback else group["traceback"]
        last_entry = group

    rows = list(groups.values())
    rows.sort(key=lambda g: g["lastSeen"] or "", reverse=True)
    rows = rows[: inputs["limit"]]
    for row in rows:
        row["traceback"] = row["traceback"][-25:]
        row["hints"] = _hints_for(row["sample"] + "\n" + "\n".join(row["traceback"]))

    outputs = {
        "log": inputs["log"],
        "path": path,
        "fileBytes": size,
        "tailBytesRead": min(size, TAIL_BYTES),
        "windowMinutes": inputs["minutes"],
        "minLevel": inputs["min_level"],
        "contains": inputs["contains"] or None,
        "entriesScanned": scanned,
        "entriesMatched": matched,
        "secretsRedacted": redacted,
        "groups": rows,
    }
    if matched == 0:
        outputs["note"] = (
            "No entries matched. Widen onb_minutes, lower onb_min_level to INFO, or try onb_log=jobengine "
            "for provisioning jobs and onb_log=authentication for logins."
        )
    message = "{} matching entr{} in {} group(s) from {} (last {} minutes).".format(
        matched, "y" if matched == 1 else "ies", len(rows), filename, inputs["minutes"]
    )
    return _success(message, outputs)
