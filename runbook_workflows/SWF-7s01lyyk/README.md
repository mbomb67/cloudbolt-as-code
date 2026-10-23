# Prepare Windows app host

Example Runbooks workflow: three PowerShell task scripts that read one JSON config file from File Archives and pass their outputs forward. It prepares a VM to host an application called `DemoApp`.

Runbooks workflow `SWF-7s01lyyk`, a member of the Runbooks package (`packages/PKG-v20qv308`). Source Control Repos does not sync workflows; the package's Sync creates it with this ID.

## Setup
1. Sync the Runbooks package from Admin > Admin Extensions > Content Packages.
2. On Admin > Admin Extensions > Runbooks, open the **File Archives** tab, click **Add archive** and name it `runbook-examples` (skip this if the Linux example already created it).
3. Open the archive, click **Upload file** and upload [windows-app-host.json](windows-app-host.json) from this folder. Keep the file name: every step attaches `runbook-examples/windows-app-host.json`.

Run it with the **Run Workflow** server action on any Windows VM, or order the **Windows VM with Workflow** blueprint (`blueprints/BP-6ddlyguv`). The VM must be able to reach CloudBolt over HTTPS to download the file.

## Steps
| Step | Script | Reads from the config file | Takes | Outputs |
|---|---|---|---|---|
| `check_host` | Check Windows App Host (`plugins/OHK-ys68wsmf`) | `checkHost` | | `os_caption`, `free_gb`, `memory_mb`, `powershell_version` |
| `create_folders` | Create Windows App Folders (`plugins/OHK-7fxxufkt`) | `appFolders` | | `app_root`, `app_group`, `items_created` |
| `configure_host` | Configure Windows App Host (`plugins/OHK-lkmy7ij9`) | `appFolders.appName`, `appSettings` | `app_root`, `app_group` from `create_folders`; `os_caption` from `check_host`; `change_ticket` asked when the workflow runs | `registry_key`, `cleanup_task`, `event_source` |

The second step creates the local group `DemoApp Operators` and `C:\DemoApp` with `bin`, `conf` and `logs`, and gives the group Modify on it. The third writes the settings and the config file's SHA-256 to `HKLM\SOFTWARE\DemoApp`, registers the daily scheduled task `\DemoApp\DemoApp log clean-up` that deletes logs older than the retention period, and writes event 1000 from source `DemoApp` to the Application log. Every step also outputs `archive_windows_app_host_json_version`, the version of the file it read.

To change a setting, edit the file and upload it again under the same name: File Archives keeps it as a new version, and the next run uses it. Running the workflow again on the same VM changes only what differs.
