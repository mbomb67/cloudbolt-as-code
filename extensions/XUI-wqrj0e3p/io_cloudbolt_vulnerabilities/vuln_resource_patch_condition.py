"""
Display Condition of the Install Updates on All Servers resource action (Vulnerabilities UI extension).

Core renders this file through its template engine and runs it every time a resource's Actions menu is built, so it
stays small. It shows the action only for a resource with a Linux or Windows server outside an Unassigned group;
anything else, and any error, hides it.
"""


def should_display(resource, request=None):
    try:
        from xui.io_cloudbolt_vulnerabilities import vuln_patch_resource

        return bool(vuln_patch_resource.has_candidates(resource))
    except Exception:
        return False
