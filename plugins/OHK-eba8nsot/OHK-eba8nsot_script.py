"""Server action: restarts Apache on a Linux server and health-checks it.

Runs one bash script on the guest: restart the unit (httpd, or apache2 where that unit exists), then up to
six checks five seconds apart that the unit is active and a local HTTP request answers with a code from 200
to 499 (with no curl, a listening port 443 or 80 stands in). OpenShift Virtualization VMs are reached through the cluster API tunnel (shared_modules/SHM-wkubiao7,
no route from CloudBolt to the guest needed); every other server goes through CloudBolt's own script
execution over SSH.

When Apache is still unhealthy and an alert started the job, shared_modules/SHM-qlvol7k8 (alert_escalation)
opens a ServiceNow incident and posts to Microsoft Teams. The alert is not a declared input: the
Monitoring Alerts inbound webhook (webhooks/IWH-i7b44zvs) passes it as an extra keyword to the job, and
someone pressing the button leaves it empty, which skips the escalation.
"""

import re

from common.methods import set_progress
from shared_modules import alert_escalation, kubevirt_api_tunnel

ACTION_NAME = "Restart Apache"
MARKER = "CB_APACHE_HEALTH="
SCRIPT_TIMEOUT_S = 600

# The script always exits 0 and ends with one CB_APACHE_HEALTH= line, so both transports are read the same way.
SCRIPT = """#!/bin/bash
unit=httpd
if systemctl list-unit-files apache2.service --no-legend 2>/dev/null | grep -q '^apache2.service'; then
  unit=apache2
fi
echo "Restarting $unit"
health=unhealthy
restart_failed=0
if ! sudo -n systemctl restart "$unit" </dev/null 2>&1; then
  echo "systemctl restart $unit failed"
  restart_failed=1
fi
have_curl=1
if ! command -v curl >/dev/null 2>&1; then
  have_curl=0
  echo "curl is not installed; checking the listening port only"
fi
if [ "$restart_failed" = "0" ]; then
  for i in 1 2 3 4 5 6; do
    state=$(systemctl is-active "$unit" </dev/null 2>&1)
    ok=0
    if [ "$have_curl" = "1" ]; then
      code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 https://localhost/ </dev/null 2>/dev/null)
      if [ -z "$code" ] || [ "$code" = "000" ]; then
        code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 http://localhost/ </dev/null 2>/dev/null)
      fi
      [ -z "$code" ] && code=000
      seen="HTTP $code"
      if [ "$code" -ge 200 ] && [ "$code" -le 499 ]; then ok=1; fi
    else
      seen="no port open"
      for port in 443 80; do
        if timeout 5 bash -c "exec 3<>/dev/tcp/127.0.0.1/$port" </dev/null 2>/dev/null; then
          seen="port $port open"
          ok=1
          break
        fi
      done
    fi
    echo "Check $i of 6: $unit is $state, $seen"
    if [ "$state" = "active" ] && [ "$ok" = "1" ]; then
      health=healthy
      break
    fi
    [ "$i" -lt 6 ] && sleep 5
  done
fi
if [ "$health" = "unhealthy" ]; then
  echo "--- systemctl status $unit"
  systemctl status "$unit" --no-pager -l </dev/null 2>&1 | tail -n 15
  echo "--- journalctl -u $unit"
  sudo -n journalctl -u "$unit" -n 15 --no-pager </dev/null 2>&1
fi
echo "CB_APACHE_HEALTH=$health"
exit 0
"""


def _run_on_server(server):
    """Run the script; returns (output, transport_error). The error is "" when the script ran."""
    try:
        if getattr(server, "ovirtserverinfo", None) is not None:
            set_progress(f"Opening the API tunnel to {server.hostname}")
            exit_code, output = kubevirt_api_tunnel.run_script(server, SCRIPT)
            return str(output or ""), ""
        # Server.execute_script (infrastructure/models.py): script_contents, run_with_sudo and timeout.
        output = server.execute_script(script_contents=SCRIPT, run_with_sudo=True, timeout=SCRIPT_TIMEOUT_S)
        return str(output or ""), ""
    except kubevirt_api_tunnel.TunnelError as error:
        return "", str(error)
    except Exception as error:
        return str(getattr(error, "output", "") or ""), f"{type(error).__name__}: {error}"


def _health(output):
    """(healthy, output without the marker line)."""
    lines = output.splitlines()
    marker = [line.strip() for line in lines if line.strip().startswith(MARKER)]
    kept = "\n".join(line for line in lines if not line.strip().startswith(MARKER)).strip()
    return bool(marker) and marker[-1] == MARKER + "healthy", kept


def run(job, server=None, alert=None, **kwargs):
    if server is None:
        return "FAILURE", "", "This action runs on a server and none was given."
    if server.is_windows():
        return "FAILURE", "", f"{server.hostname} is a Windows server; {ACTION_NAME} works on Linux servers only."

    set_progress(f"Restarting Apache on {server.hostname}")
    raw, transport_error = _run_on_server(server)
    healthy, output = _health(raw)
    if transport_error:
        healthy = False
        output = (output + "\n" if output else "") + transport_error
    elif not healthy and MARKER not in raw:
        output = (output + "\n" if output else "") + "The server returned no health result."
    if output:
        set_progress(output)

    if healthy:
        unit = "apache2" if "apache2" in output else "httpd"
        seen = re.findall(r"is active, (HTTP \d{3}|port \d+ open)", output)
        passed = f" ({seen[-1]})" if seen else ""
        return "SUCCESS", f"{unit} restarted on {server.hostname}; health check passed{passed}.", ""

    message = f"Apache is still unhealthy on {server.hostname} after the restart."
    if transport_error and MARKER not in raw:
        # The script never reported back, so nothing is known about Apache itself.
        message = f"CloudBolt could not run the restart on {server.hostname}."
        output = (
            f"{output}\nThe script did not run to the end on {server.hostname}. Check that the server is up "
            "and that CloudBolt has working credentials for it."
        )
        set_progress(message)
    if isinstance(alert, dict) and alert:
        result = alert_escalation.escalate(server, alert, output, job=job, action=ACTION_NAME)
        output = output + "\n\n" + "\n".join(result["notes"])
    return "FAILURE", output, message
