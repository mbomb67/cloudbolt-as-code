# Prepare Linux app host

Example Runbooks workflow: three Linux task scripts that read one config file from File Archives and pass their outputs forward. It prepares a VM to host an application called `demoapp`.

Runbooks workflow `SWF-69r5mmdb`, a member of the Runbooks package (`packages/PKG-v20qv308`). Source Control Repos does not sync workflows; the package's Sync creates it with this ID.

## Setup
1. Sync the Runbooks package from Admin > Admin Extensions > Content Packages.
2. On Admin > Admin Extensions > Runbooks, open the **File Archives** tab, click **Add archive** and name it `runbook-examples`.
3. Open the archive, click **Upload file** and upload [linux-app-host.conf](linux-app-host.conf) from this folder. Keep the file name: every step attaches `runbook-examples/linux-app-host.conf`.

Run it with the **Run Workflow** server action on any Linux VM, or order the **Linux VM with Workflow** blueprint (`blueprints/BP-vy9ytey6`). The VM must be able to reach CloudBolt over HTTPS to download the file.

## Steps
| Step | Script | Reads from the config file | Takes | Outputs |
|---|---|---|---|---|
| `check_host` | Check Linux App Host (`plugins/OHK-fct1gybi`) | `MIN_FREE_GB`, `MIN_MEMORY_MB`, `DATA_MOUNT`, `REQUIRED_COMMANDS` | | `os_name`, `free_gb`, `memory_mb`, `package_manager` |
| `create_account` | Create Linux App Account (`plugins/OHK-4etpeg05`) | `APP_NAME`, `APP_USER`, `APP_GROUP`, `APP_HOME` | | `app_user`, `app_group`, `app_uid`, `app_home`, `account` |
| `configure_host` | Configure Linux App Host (`plugins/OHK-o30l8kjn`) | `APP_NAME`, `LOG_RETENTION_DAYS`, `ENVIRONMENT`, `OWNER_TEAM` | `app_user`, `app_home` from `create_account`; `os_name` from `check_host`; `change_ticket` asked when the workflow runs | `cleanup_rule`, `profile_file`, `log_retention_days` |

The third step writes `/etc/tmpfiles.d/demoapp.conf`, which deletes files in `/opt/demoapp/logs` older than the retention period, and `/opt/demoapp/conf/host-profile.conf`, which records the settings and the config file's SHA-256. Every step also outputs `archive_linux_app_host_conf_version`, the version of the file it read.

To change a setting, edit the file and upload it again under the same name: File Archives keeps it as a new version, and the next run uses it. Running the workflow again on the same VM changes only what differs.
