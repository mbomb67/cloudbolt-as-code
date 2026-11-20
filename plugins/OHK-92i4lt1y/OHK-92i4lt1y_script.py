"""Blueprint build item: installs MariaDB on a server tier and creates the application's database.

Runs on the VM through the OpenShift API tunnel (shared_modules/SHM-wkubiao7). The database user may
connect from any host, because traffic from another VM arrives from a pod IP that changes; who can
reach port 3306 is decided by a NetworkPolicy instead.

This is the first step that needs the database password, so it asks for it and then stores it,
encrypted, on the resource as web_app_db_password, where the steps that connect to the database
read it. Run again without a password, it reuses the stored one.

Inputs: tier (the server build item's name, default DB), database_name, database_user,
database_password.
"""

import base64
import re

from common.methods import set_progress
from shared_modules import vm_tier_scripts as tiers

TIER = tiers.clean("{{ tier }}", "DB")
DATABASE = tiers.clean("{{ database_name }}", "requestlog")
USER = tiers.clean("{{ database_user }}", "requestlog")
# CloudBolt escapes backslashes and quotes in a string input before it renders, so any password
# is a valid literal here, and it keeps password inputs out of the job's logged parameters.
PASSWORD = "{{ database_password }}"
PASSWORD_PARAMETER = "web_app_db_password"

IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")


def _sql_string(value):
    """A MariaDB string literal: https://mariadb.com/kb/en/string-literals/"""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def run(job, resource=None, **kwargs):
    for label, value in (("Database name", DATABASE), ("Database user", USER)):
        if not IDENTIFIER.match(value):
            return "FAILURE", "", f"{label} '{value}' must be letters, digits and underscores, starting with a letter."
    try:
        server = tiers.tier_server(resource, TIER)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)

    password = "" if PASSWORD in ("", "None") else PASSWORD
    if password:
        tiers.remember(resource, PASSWORD_PARAMETER, password, "Database Password", "PWD")
    else:
        password = resource.get_value_for_custom_field(PASSWORD_PARAMETER)
    if not password:
        return "FAILURE", "", "Enter a Database Password for this step; none is stored on the resource yet."

    sql = f"""
CREATE DATABASE IF NOT EXISTS `{DATABASE}` CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS '{USER}'@'%' IDENTIFIED BY {_sql_string(password)};
ALTER USER '{USER}'@'%' IDENTIFIED BY {_sql_string(password)};
GRANT ALL PRIVILEGES ON `{DATABASE}`.* TO '{USER}'@'%';
CREATE TABLE IF NOT EXISTS `{DATABASE}`.entries (
    id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    message VARCHAR(500) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
FLUSH PRIVILEGES;
"""
    script = (
        tiers.ENABLE_PACKAGE_REPOS
        + "sudo dnf -y install mariadb-server\n"
        + tiers.write_file("/etc/my.cnf.d/zz-listen.cnf", "[mysqld]\nbind-address=0.0.0.0\n")
        + "sudo systemctl enable mariadb\n"
        + "sudo systemctl restart mariadb\n"
        + f"echo '{base64.b64encode(sql.encode()).decode()}' | base64 -d | sudo mysql\n"
        + "echo '== Installed'\n"
        + "rpm -q mariadb-server\n"
        + 'echo "mariadb is $(systemctl is-active mariadb)"\n'
        + "sudo ss -ltn | grep ':3306 '\n"
        + f"sudo mysql -N -e \"SHOW TABLES IN \\`{DATABASE}\\`\"\n"
    )

    set_progress(f"Installing MariaDB on {server.hostname} and creating database {DATABASE}")
    try:
        tiers.run(server, script)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)
    return "SUCCESS", f"MariaDB is running on {server.hostname} with database {DATABASE} and user {USER}.", ""
