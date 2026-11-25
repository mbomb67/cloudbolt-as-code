"""Blueprint build item: installs Apache httpd and PHP on a server tier.

Runs on the VM through the OpenShift API tunnel (shared_modules/SHM-wkubiao7). With the PHP stream
set to the word default, or left empty, the distribution's own PHP is installed (8.0 on Enterprise
Linux 9), which leaves a newer stream to move to later: run this again with php_stream set, for
example 8.3.

Inputs: tier (the server build item's name, default App), php_stream (default: default),
http_port (default 8080).
"""

import re

from common.methods import set_progress
from shared_modules import vm_tier_scripts as tiers

TIER = tiers.clean("{{ tier }}", "App")
PHP_STREAM = tiers.clean("{{ php_stream }}")
if PHP_STREAM.lower() == "default":
    PHP_STREAM = ""
HTTP_PORT = tiers.clean("{{ http_port }}", "8080")


def run(job, resource=None, **kwargs):
    if PHP_STREAM and not re.match(r"^\d+\.\d+$", PHP_STREAM):
        return "FAILURE", "", f"PHP stream '{PHP_STREAM}' must look like 8.3, or be the word default."
    if not HTTP_PORT.isdigit():
        return "FAILURE", "", f"HTTP port '{HTTP_PORT}' must be a number."
    try:
        server = tiers.tier_server(resource, TIER)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)

    script = tiers.ENABLE_PACKAGE_REPOS
    if PHP_STREAM:
        # https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/managing_software_with_the_dnf_tool/assembly_managing-versions-of-application-stream-content_managing-software-with-the-dnf-tool
        script += f"sudo dnf -y module reset php\nsudo dnf -y module enable php:{PHP_STREAM}\n"
    script += (
        "sudo dnf -y install httpd php php-cli php-mysqlnd\n"
        + ("sudo dnf -y distro-sync 'php*'\n" if PHP_STREAM else "")
        + f"sudo sed -i -E 's/^Listen .*/Listen {HTTP_PORT}/' /etc/httpd/conf/httpd.conf\n"
        # Lets PHP open a connection to a database on another host.
        + "sudo setsebool -P httpd_can_network_connect_db 1\n"
        + "sudo systemctl enable php-fpm httpd\n"
        + "sudo systemctl restart php-fpm httpd\n"
        + "echo '== Installed'\n"
        + "rpm -q httpd php\n"
        + "php -v | head -1\n"
        + 'echo "httpd is $(systemctl is-active httpd), php-fpm is $(systemctl is-active php-fpm)"\n'
        + f"sudo ss -ltn | grep ':{HTTP_PORT} '\n"
    )

    set_progress(f"Installing Apache and PHP {PHP_STREAM or '(distribution default)'} on {server.hostname}")
    try:
        output = tiers.run(server, script)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)
    version = re.search(r"^PHP (\S+)", output, re.MULTILINE)
    return (
        "SUCCESS",
        f"Apache is serving on port {HTTP_PORT} of {server.hostname} with PHP {version.group(1) if version else 'installed'}.",
        "",
    )
