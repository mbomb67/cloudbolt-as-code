from django.urls import path

from . import views

# The tab loads its panel and its findings asynchronously, and every route that takes a
# server id re-checks the viewer's permission on it. The upload and tool routes are the two
# routes without a CloudBolt login: the signed token in their X-Vuln-Token header is the
# credential, and servers use them only to send their software list and fetch the inventory tool.
xui_urlpatterns = [
    # HTML fragment the tab loads
    path(
        "vulnerabilities/servers/<int:server_id>/panel/",
        views.vuln_panel,
        name="vuln_panel",
    ),
    # JSON: one page of the packages or CVEs table, filtered on the server
    path(
        "vulnerabilities/servers/<int:server_id>/findings/",
        views.vuln_findings,
        name="vuln_findings",
    ),
    # JSON: collect the software list and match it, as a job
    path(
        "vulnerabilities/servers/<int:server_id>/scan/",
        views.vuln_start,
        name="vuln_start",
    ),
    # JSON: install or update the scanner (CloudBolt admins only)
    path(
        "vulnerabilities/setup/",
        views.vuln_setup,
        name="vuln_setup",
    ),
    # Servers send their software list here with a signed token
    path(
        "vulnerabilities/upload/<str:scan_id>/<str:name>",
        views.vuln_upload,
        name="vuln_upload",
    ),
    # Servers fetch the inventory tool from here with a signed token
    path(
        "vulnerabilities/tools/<str:scan_id>/<str:name>",
        views.vuln_tool,
        name="vuln_tool",
    ),
]
