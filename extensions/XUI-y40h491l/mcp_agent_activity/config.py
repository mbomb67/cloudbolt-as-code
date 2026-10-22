"""
Settings for the Agent Activity page.

Each product decision is one constant here, so changing it is a one-line
change followed by a sync of this extension and an httpd restart. The values
below are the ones Mike chose on 2026-10-20.
"""

# Decision 1, who opens the page and what they see. "scoped": every signed-in
# user opens it; CB Admins and Super Admins (and Global Viewers, see below)
# see every user's records, everyone else only their own. "admin": only those
# who see every record open it.
VISIBILITY = "scoped"

# Decision 11, Global Viewers see every user's records and the MCP log, like
# CB Admins (the security persona). False puts them in the own-records scope.
GLOBAL_VIEWERS_SEE_ALL = True

# Decision 2, what the page shows before the first search. "recent_sessions":
# the newest sessions in the default window, a page at a time. "empty":
# nothing until Search is pressed.
DEFAULT_VIEW = "recent_sessions"

# Decision 3, the MCP log section on a session's detail: lines from mcp.log
# that name the session. Users who see only their own records get only the
# lines logged under their own username while MCP_LOG_OWN_LINES is True, and
# no log section when it is False.
MCP_LOG_ENABLED = True
MCP_LOG_OWN_LINES = True
MCP_LOG_MAX_LINES = 500
# The newest bytes of mcp.log read per request, so an old session cannot make
# the page read the whole file. Rotated files (mcp.log.1, ...) are not read,
# and on a multi-node install only the web node serving the page is read.
MCP_LOG_MAX_BYTES = 64 * 1024 * 1024

# The client application behind a session (MCP initialize clientInfo, which
# the patch writes to mcp.log only) is shown next to the agent when the viewer
# may read the log. The agent in the marker is the OAuth client's registered
# name, which a bridge such as mcp-remote sets to "MCP CLI Proxy" whatever
# application sits behind it.
CLIENT_APP_FROM_LOG = True
# Friendlier names for clientInfo names, matched by prefix, first match wins.
KNOWN_CLIENTS = (
    ("local-agent-mode", "Claude Desktop (local agent)"),
    ("claude-code", "Claude Code"),
    ("claude-ai", "Claude"),
)

# Decision 4, the date window in days: the default when no dates are given,
# and the largest window any request may ask for (enforced server side).
DEFAULT_DAYS = 7
MAX_DAYS = 90

# Decision 5, how the user, agent and session filters narrow each other.
# "any": every selection narrows the other two lists. "cascade": user narrows
# agent, user and agent narrow session, and a later choice never narrows an
# earlier list.
FACET_MODE = "any"
FACET_ORDER = ("user", "agent", "session")

# Decision 6, whether an order's recipient ("on behalf of" user) is a filter
# like the owner. False: shown in its own column only.
RECIPIENT_IS_PERSONA = False

# Decision 7, CSV export of each result section.
CSV_EXPORT_ENABLED = True
# One Excel workbook of a search or a session: Summary, Orders, Jobs, Server and
# resource history, and (for a session, when the viewer may read the log) Tool calls.
XLSX_EXPORT_ENABLED = True

# Decision 12, caching: none. Every search, filter change, page turn and CSV
# download reads the window again.

# Rows per page in every table, the recent sessions included.
PAGE_SIZE = 20

# The newest rows read from each source (order history, job progress
# messages, other history) per search. A search that hits it says so.
MAX_ROWS_PER_SOURCE = 20000

# Sessions listed in the Session filter, newest first. Older ones can still be
# found by typing the session id in the text box.
MAX_SESSION_OPTIONS = 200
