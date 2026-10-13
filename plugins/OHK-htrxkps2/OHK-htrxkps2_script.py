"""
Close the ServiceNow RITM when a CloudBolt order finishes and attach the CIs.

Bound to "Post-Order Execution", which fires once every job of the order has
finished. The RITM is closed complete for SUCCESS and WARNING orders and
closed incomplete for FAILURE. Every server the order produced is looked up
in the CMDB (the CI CloudBolt's CMDB integration created at Post-Provision)
and added to the RITM's Affected CIs; the first one becomes the RITM's
Configuration item. Resources are attached when they carry the custom field
"servicenow_ci_sys_id". The ServiceNow identifiers are also written onto the
created servers and resources so day-2 and teardown orders can refer back to
the originating request.

Inputs (pinned on the orchestration action):
  servicenow_connection_name  ConnectionInfo name (default "ServiceNow").
  servicenow_ci_table         CMDB table searched for server CIs (default "cmdb_ci_server").
  cloudbolt_base_url          Base URL for links in the work note; empty = default portal.
"""
from utilities.logger import ThreadLogger

import servicenow_requests as snr

logger = ThreadLogger(__name__)

CONNECTION_NAME = "{{ servicenow_connection_name }}".strip() or snr.DEFAULT_CONNECTION_NAME
CI_TABLE = "{{ servicenow_ci_table }}".strip() or snr.DEFAULT_CI_TABLE
BASE_URL = "{{ cloudbolt_base_url }}".strip()


def run(order=None, job=None, logger=logger, **kwargs):
    if order is None:
        return "SUCCESS", "No order in context; nothing to do.", ""

    ids = snr.load_request_ids(order)
    if not ids or not ids.get("ritm_sys_id"):
        return "SUCCESS", f"Order {order.id} has no ServiceNow RITM; nothing to close.", ""
    if ids.get("closed"):
        return "SUCCESS", f"ServiceNow {ids.get('ritm_number')} is already closed.", ""

    status = order.status
    state = (
        snr.RITM_STATE_CLOSED_COMPLETE
        if status in ("SUCCESS", "WARNING")
        else snr.RITM_STATE_CLOSED_INCOMPLETE
    )
    servers = [s for s in snr.order_servers(order) if s.status != "HISTORICAL"]
    resources = snr.order_resources(order)

    note_lines = [f"CloudBolt order {order.id} finished with status {status}."]
    note_lines.append(f"Order: {snr.order_url(order, BASE_URL)}")
    for resource in resources:
        note_lines.append(f"Resource {resource.name}: {snr.object_url(resource, BASE_URL)}")
    for server in servers:
        note_lines.append(f"Server {server.hostname}: {snr.object_url(server, BASE_URL)}")

    linked = []
    try:
        with snr.ServiceNowRequestClient(CONNECTION_NAME) as snow:
            for server in servers:
                ci = snow.find_server_ci(server, CI_TABLE)
                if ci:
                    snow.link_ci(ids["ritm_sys_id"], ci)
                    linked.append((server.hostname, ci))
                else:
                    note_lines.append(f"No CI found in {CI_TABLE} for {server.hostname}.")
            for resource in resources:
                ci = resource.get_value_for_custom_field(snr.CF_RESOURCE_CI_SYS_ID)
                if ci:
                    snow.link_ci(ids["ritm_sys_id"], ci)
                    linked.append((resource.name, ci))
            if linked:
                note_lines.append("Affected CIs: " + ", ".join(name for name, _ in linked))
            extra = {"cmdb_ci": linked[0][1]} if linked else {}
            close_notes = (
                f"Fulfilled by CloudBolt (order {order.id}, status {status})."
                if state == snr.RITM_STATE_CLOSED_COMPLETE
                else f"CloudBolt order {order.id} failed; see the order for job logs."
            )
            snow.close_ritm(ids["ritm_sys_id"], state, close_notes, "\n".join(note_lines), extra)
    except snr.ServiceNowError as err:
        return "FAILURE", "", f"Could not close ServiceNow {ids.get('ritm_number')}: {err}"

    ids["closed"] = True
    ids["order_status"] = status
    snr.store_request_ids(order, ids)
    snr.store_request_ids(order, ids, targets=servers + resources)

    msg = (
        f"ServiceNow {ids.get('ritm_number')} closed "
        f"{'complete' if state == snr.RITM_STATE_CLOSED_COMPLETE else 'incomplete'} "
        f"with {len(linked)} CI(s) attached."
    )
    snr.record_event(order, msg)
    snr.progress(msg, job)
    return "SUCCESS", msg, ""
