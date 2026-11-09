"""
Who sees what (decisions 1, 3 and 11 in config.py).

Two scopes: CB Admins, Super Admins and, with GLOBAL_VIEWERS_SEE_ALL, Global
Viewers see every user's records; everyone else sees only their own (orders
they own or acted on, jobs they own, history written under their name).
"""
from . import config


def sees_all(profile):
    return bool(profile.is_cbadmin or (config.GLOBAL_VIEWERS_SEE_ALL and profile.global_viewer))


def may_view(profile):
    """Whether this profile may open the page and its endpoints."""
    return config.VISIBILITY == "scoped" or sees_all(profile)


def log_username(profile):
    """
    None when the profile may read every mcp.log line, its username when it may
    read only its own lines, and False when it may not read the log.
    """
    if not config.MCP_LOG_ENABLED or not may_view(profile):
        return False
    if sees_all(profile):
        return None
    return profile.user.username if config.MCP_LOG_OWN_LINES else False
