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
    # JSON for the Vulnerabilities admin page (CloudBolt admins only), one page at a time
    path(
        "vulnerabilities/fleet/summary/",
        views.vuln_fleet_summary,
        name="vuln_fleet_summary",
    ),
    path(
        "vulnerabilities/fleet/cves/",
        views.vuln_fleet_cves,
        name="vuln_fleet_cves",
    ),
    path(
        "vulnerabilities/fleet/cve-servers/",
        views.vuln_fleet_cve_servers,
        name="vuln_fleet_cve_servers",
    ),
    path(
        "vulnerabilities/fleet/servers/",
        views.vuln_fleet_servers,
        name="vuln_fleet_servers",
    ),
    # JSON: the values the Servers view's filters offer, and the ids "Select all" picks
    path(
        "vulnerabilities/fleet/server-filters/",
        views.vuln_fleet_server_filters,
        name="vuln_fleet_server_filters",
    ),
    path(
        "vulnerabilities/fleet/servers/select/",
        views.vuln_fleet_servers_select,
        name="vuln_fleet_servers_select",
    ),
    # POST: start one Install Updates run for the selected servers (CloudBolt admins only)
    path(
        "vulnerabilities/fleet/servers/patch/",
        views.vuln_fleet_servers_patch,
        name="vuln_fleet_servers_patch",
    ),
    # JSON for a resource's Vulnerabilities tab. Each route re-checks that the viewer may see the
    # resource and works only on the servers of it that the viewer may see.
    path(
        "vulnerabilities/resources/<int:resource_id>/summary/",
        views.vuln_resource_summary,
        name="vuln_resource_summary",
    ),
    path(
        "vulnerabilities/resources/<int:resource_id>/cves/",
        views.vuln_resource_cves,
        name="vuln_resource_cves",
    ),
    path(
        "vulnerabilities/resources/<int:resource_id>/cve-servers/",
        views.vuln_resource_cve_servers,
        name="vuln_resource_cve_servers",
    ),
    path(
        "vulnerabilities/resources/<int:resource_id>/servers/",
        views.vuln_resource_servers,
        name="vuln_resource_servers",
    ),
    # JSON: scan the servers of the resource the viewer may scan, as one job
    path(
        "vulnerabilities/resources/<int:resource_id>/scan/",
        views.vuln_resource_scan,
        name="vuln_resource_scan",
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
