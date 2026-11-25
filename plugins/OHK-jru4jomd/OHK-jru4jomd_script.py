"""Blueprint build item: proves a 3-tier web app works end to end and reports its URL.

1. From the web tier, through the OpenShift API tunnel: adds an entry through the app and reads it
   back, which exercises web to app to database and shows the tunnel survived the NetworkPolicies.
2. From the web tier: confirms it cannot open the database port itself.
3. From CloudBolt: opens the public URL stored on the resource (web_app_url).

Inputs: web_tier (default Web), db_tier (default DB), db_port (default 3306).
"""

import time

import requests

from common.methods import set_progress
from shared_modules import openshift_vm_network as vmnet
from shared_modules import vm_tier_scripts as tiers
from utilities.helpers import get_ssl_verification

WEB_TIER = tiers.clean("{{ web_tier }}", "Web")
DB_TIER = tiers.clean("{{ db_tier }}", "DB")
DB_PORT = tiers.clean("{{ db_port }}", "3306")
URL_TIMEOUT_SECONDS = 120


def run(job, resource=None, **kwargs):
    try:
        web = tiers.tier_server(resource, WEB_TIER)
        db = tiers.tier_server(resource, DB_TIER)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)
    db_host = vmnet.service_fqdn(db.hostname, db.ovirtserverinfo.namespace)
    message = f"Deployment verified by CloudBolt job {job.id}"

    script = (
        "curl -sk --max-time 20 -o /dev/null -X POST https://localhost/"
        f" --data-urlencode 'name=CloudBolt' --data-urlencode 'message={message}'\n"
        + "page=$(curl -sk --max-time 20 https://localhost/)\n"
        + f"echo \"$page\" | grep -q '{message}'\n"
        + "echo 'An entry added through the web tier was stored in the database and listed back.'\n"
        + "echo \"$page\" | grep -o 'Served by.*' | sed 's/<[^>]*>//g'\n"
        + f"if timeout 5 bash -c '</dev/tcp/{db_host}/{DB_PORT}' 2>/dev/null; then\n"
        + f"  echo 'The web tier can open {db_host}:{DB_PORT}; it should be blocked.'\n"
        + "  exit 1\n"
        + "fi\n"
        + f"echo 'The web tier cannot open {db_host}:{DB_PORT}, as intended.'\n"
    )
    set_progress(f"Checking the app from {web.hostname}")
    try:
        tiers.run(web, script)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)

    url = resource.get_value_for_custom_field("web_app_url")
    if not url:
        return "SUCCESS", "The app works inside the cluster. It has no public URL.", ""

    # The router takes a few seconds to pick up a Route change.
    set_progress(f"Opening {url} from CloudBolt")
    deadline = time.time() + URL_TIMEOUT_SECONDS
    problem = ""
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=20, verify=get_ssl_verification())
            if response.status_code == 200 and message in response.text:
                set_progress(f"{url} answered 200 with the app's page")
                return "SUCCESS", f"The app is live at {url}", ""
            problem = f"it answered {response.status_code}"
        except requests.exceptions.RequestException as error:
            problem = str(error)
        time.sleep(10)
    return "FAILURE", "", f"The app works inside the cluster, but {url} did not serve it within {URL_TIMEOUT_SECONDS} seconds: {problem}"
