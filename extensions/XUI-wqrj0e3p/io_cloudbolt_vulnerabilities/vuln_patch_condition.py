"""
Display Condition of the Install Updates server action (Vulnerabilities UI extension).

Core renders this file through its template engine and runs it every time a server's Actions menu is built,
so it stays small. It shows the action only for Linux and Windows servers outside an Unassigned group;
anything else, and any error, hides it.
"""


def should_display(server, request=None):
    try:
        from xui.io_cloudbolt_vulnerabilities import vuln_jobs

        return bool((vuln_jobs.is_linux(server) or vuln_jobs.is_windows(server))
                    and not vuln_jobs.in_unassigned_group(server))
    except Exception:
        return False
