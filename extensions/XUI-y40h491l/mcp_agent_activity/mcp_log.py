"""
CloudBolt's MCP tool log (mcp.log): the tool calls of one session, and the
client application behind each session.

With the attribution patch, each MCP tool call logs a line

    <asctime> [INFO] mcp.tools: <correlation id> <username> <tool> agent="..." client="..."
        session="<sid>" jti="..." client_info="<name>/<version>" args=(...) kwargs={...}

and a second line with the same prefix ending "completed in Nms" or
"failed in Nms: <error>". client_info is the MCP initialize clientInfo, the
application that opened the connection (for example
"local-agent-mode-<server> (via mcp-remote 0.15.2)/1.0.0"); it is empty for a
connection opened before the patch was loaded. The file is read backwards
from its end, at most MCP_LOG_MAX_BYTES.
"""
import logging
import os
import re

from django.conf import settings

from . import config, markers

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/var/log/cloudbolt/mcp.log"
BLOCK = 256 * 1024
LINE_CHARS = 4000

LINE_RE = re.compile(
    r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:,\d+)? \[(?P<level>\w+)\] \S+: "
    r"(?P<call>[0-9a-f]{32}) (?P<user>\S+) (?P<tool>\S+) (?P<rest>.*)$"
)
DONE_RE = re.compile(r" (?P<outcome>completed|failed) in (?P<ms>\d+)ms(?:: (?P<error>.*))?$")
CLIENT_INFO_RE = re.compile(r'client_info="((?:[^"\\]|\\.)*)"')
SESSION_RE = re.compile(r'session="([0-9a-f]{0,12})"')
ARGS_RE = re.compile(r" args=(?P<args>.*)$")
# run_mcp_tool_action names the MCP Tool Action it runs in its kwargs.
ACTION_RE = re.compile(r"'mcp_tool_name': '([^']+)'")
BRIDGE_RE = re.compile(r"^(?P<name>.*?) \(via (?P<bridge>[^)]+)\)$")


def log_path():
    try:
        return settings.LOGGING["handlers"]["mcp_logfile"]["filename"]
    except (AttributeError, KeyError, TypeError):
        return DEFAULT_PATH


def _reverse_lines(handle, size, max_bytes):
    """Yield complete lines from the end of the file backwards; the last value is the bytes read."""
    position = size
    read = 0
    carry = b""
    while position > 0 and read < max_bytes:
        step = min(BLOCK, position, max_bytes - read)
        position -= step
        handle.seek(position)
        chunk = handle.read(step) + carry
        read += step
        lines = chunk.split(b"\n")
        # The first piece may be the end of a line that starts in the next block back.
        carry = lines.pop(0) if position > 0 else b""
        for line in reversed(lines):
            yield line
    yield read


def _tail_lines(handle, size, needle, limit, max_bytes, keep=None):
    """Lines containing needle (and passing keep), newest first, and the number of bytes read."""
    found = []
    read = 0
    for line in _reverse_lines(handle, size, max_bytes):
        if isinstance(line, int):
            read = line
            break
        if needle in line and (keep is None or keep(line)):
            found.append(line)
            if len(found) >= limit:
                break
    return found, read


def _unescape(value):
    return re.sub(r"\\(.)", r"\1", value)


def client_app(client_info):
    """{"raw", "name", "version", "bridge", "label"} from a client_info value, or None when empty."""
    if not client_info:
        return None
    name, version = client_info, ""
    head, _, tail = client_info.rpartition("/")
    if head and tail and " " not in tail:
        name, version = head, tail
    bridge = ""
    match = BRIDGE_RE.match(name)
    if match:
        name, bridge = match.group("name"), match.group("bridge")
    label = name
    for prefix, friendly in config.KNOWN_CLIENTS:
        if name.lower().startswith(prefix.lower()):
            label = friendly
            break
    return {"raw": client_info, "name": name, "version": version, "bridge": bridge, "label": label}


def parse_line(raw):
    text = raw.decode("utf-8", "replace").rstrip("\r")
    if len(text) > LINE_CHARS:
        text = text[:LINE_CHARS] + "…"
    line = {"raw": text, "time": "", "level": "", "call": "", "user": "", "tool": "", "outcome": "", "ms": None,
            "error": "", "client_info": "", "action": "", "args": ""}
    match = LINE_RE.match(text)
    if not match:
        return line
    line.update({key: match.group(key) for key in ("time", "level", "call", "user", "tool")})
    rest = match.group("rest")
    info = CLIENT_INFO_RE.search(rest)
    if info:
        line["client_info"] = _unescape(info.group(1))
    done = DONE_RE.search(rest)
    if done:
        line["outcome"] = done.group("outcome")
        line["ms"] = int(done.group("ms"))
        line["error"] = done.group("error") or ""
    args = ARGS_RE.search(rest)
    if args:
        line["args"] = args.group("args")
        action = ACTION_RE.search(line["args"])
        if action:
            line["action"] = action.group(1)
    return line


def _calls(lines):
    """One entry per tool call (oldest first), joining its start and end lines by correlation id."""
    calls = {}
    order = []
    for line in lines:
        key = line["call"] or line["raw"]
        call = calls.get(key)
        if call is None:
            call = calls[key] = dict(line, done_time="")
            order.append(key)
        if line["outcome"]:
            call.update(outcome=line["outcome"], ms=line["ms"], error=line["error"], done_time=line["time"])
        if line["args"]:
            call.update(args=line["args"], action=line["action"], time=line["time"], raw=line["raw"])
    return [calls[key] for key in order]


def session_lines(session, username=None):
    """
    {"path", "lines", "calls", "client_app", "limited", "error"} for one session, oldest
    first; only the lines logged under username when one is given.
    """
    path = log_path()
    result = {"path": path, "lines": [], "calls": [], "client_app": None, "limited": False, "error": ""}
    if not markers.is_session(session):
        result["error"] = "Not a session id."
        return result
    needle = 'session="{}"'.format(session).encode("ascii")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            keep = (lambda raw: parse_line(raw)["user"] == username) if username else None
            found, read = _tail_lines(
                handle, size, needle, config.MCP_LOG_MAX_LINES, config.MCP_LOG_MAX_BYTES, keep=keep
            )
    except FileNotFoundError:
        result["error"] = "{} does not exist on this CloudBolt server.".format(path)
        return result
    except OSError:
        logger.exception("Agent Activity could not read %s", path)
        result["error"] = "{} could not be read; see the CloudBolt application log.".format(path)
        return result
    result["lines"] = [parse_line(raw) for raw in reversed(found)]
    result["calls"] = _calls(result["lines"])
    infos = [line["client_info"] for line in result["lines"] if line["client_info"]]
    result["client_app"] = client_app(infos[-1]) if infos else None
    result["limited"] = len(found) >= config.MCP_LOG_MAX_LINES or read < size
    return result


def client_apps(sessions, username=None):
    """
    {session: client app} for the given sessions, from their newest log line with a
    client_info. One backward pass that stops once every session is found.
    """
    wanted = {session for session in sessions if markers.is_session(session)}
    found = {}
    if not wanted:
        return found
    try:
        size = os.path.getsize(log_path())
        with open(log_path(), "rb") as handle:
            for raw in _reverse_lines(handle, size, config.MCP_LOG_MAX_BYTES):
                if isinstance(raw, int) or not wanted - set(found):
                    break
                if b'client_info="' not in raw or b'client_info=""' in raw:
                    continue
                session = SESSION_RE.search(raw.decode("utf-8", "replace"))
                if not session or session.group(1) not in wanted or session.group(1) in found:
                    continue
                line = parse_line(raw)
                if username and line["user"] != username:
                    continue
                app = client_app(line["client_info"])
                if app:
                    found[session.group(1)] = app
    except OSError:
        return found
    return found
