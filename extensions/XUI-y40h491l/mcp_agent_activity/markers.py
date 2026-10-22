"""
The MCP attribution marker that CloudBolt's MCP agent attribution patch
(ps_008) writes into audit records:

    [mcp agent="<agent>" client="<client_id>" session="<sid>"]

Values are double-quoted with backslash and double quote escaped by a
backslash. agent is the OAuth application name of the client (or its MCP
clientInfo name, its User-Agent, or "unknown"), client its OAuth client id or
"", and session the first 12 hex characters of sha256 of the Mcp-Session-Id
header (of the token's jti when there is no header).

The patch appends the marker, after one space, to history event messages, and
starts a job progress message with it. Text before the marker can come from
users (order, server and resource names), so a history message is read by its
last marker and a progress message only by a leading one.
"""
import re
from collections import namedtuple

PREFIX = '[mcp agent="'

MARKER_RE = re.compile(
    r'\[mcp agent="((?:[^"\\]|\\.)*)" client="((?:[^"\\]|\\.)*)" session="([0-9a-f]{0,12})"\]'
)
SESSION_RE = re.compile(r"^[0-9a-f]{1,12}$")

Marker = namedtuple("Marker", "agent client session raw")


def _unescape(value):
    return re.sub(r"\\(.)", r"\1", value)


def _marker(match):
    return Marker(_unescape(match.group(1)), _unescape(match.group(2)), match.group(3), match.group(0))


def last_marker(text):
    """The last marker in a history event message, or None."""
    found = None
    for match in MARKER_RE.finditer(text or ""):
        found = match
    return _marker(found) if found else None


def leading_marker(text):
    """The marker a job progress message starts with, or None."""
    match = MARKER_RE.match(text or "")
    return _marker(match) if match else None


def strip_markers(text):
    """The message without its markers."""
    return MARKER_RE.sub("", text or "").strip()


def session_needle(session):
    """A substring that matches the marker of exactly this session, for a database filter."""
    return 'session="{}"]'.format(session)


def is_session(value):
    return bool(SESSION_RE.match(value or ""))
