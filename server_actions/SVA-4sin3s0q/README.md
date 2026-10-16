# Run Task

One server action for every Remote Script that fits a server. Its form lists the Remote Scripts that can run on the selected server, shows the chosen script's own inputs, and runs it there with the script's own transport, timeout and credentials. New tasks are new Remote Scripts, not new buttons.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-o4tgygjs | Run Task |
| Custom form | FRM-a5edrdj4 | Run Task |
| Form function | FJS-m3awusu7 | serverTaskForm |
| Shared module | SHM-571l7249 | server_tasks |
| Inbound webhook | IWH-cdf543g3 | Server Tasks (`server-tasks`), called by the form |
| Remote Script | OHK-lhm2916o | Server Task Template (Windows) |
| Remote Script | OHK-iub9a2jg | Server Task Template (Linux) |
| Remote Script | OHK-01kmavro | Manage Windows Service |
| Remote Script | OHK-5yh12jiq | Manage Linux Service |

## Setup
1. Sync the Server Tasks package (`packages/PKG-93egd3on`), or sync `server_actions/`, `webhooks/` and the four Remote Scripts. A server action sync does not bring the webhook or the Remote Scripts.
2. Restart the CloudBolt web service after the first sync and after any change to `server_tasks`: the form's webhook and the task list run in the web process, which keeps the old shared module until it restarts.
3. Give the groups that should run tasks permission to run this server action.

## Which tasks a server lists
Every Remote Script that passes the checks CloudBolt makes before it runs a Remote Script on a server:
- no Run on Server set;
- the server's resource handler can run scripts;
- the script's OS families, resource technologies, environments, groups and configuration managers are empty or include the server's (an OS family includes its children, so Windows covers Windows 2022).

A bulk run lists only the tasks that fit every selected server. Build-step scripts are listed too; restrict a script with its OS families, groups or environments to keep it off servers.

## Credentials
The task uses the Remote Script's saved credentials, else the server's. Azure and AWS servers with tech-specific script execution (Run Command, SSM) need none. When CloudBolt has no way to log in, the form shows a username and password box that saves them on the server, encrypted, as CloudBolt's own credential prompt does. Saving needs the Manage Credentials permission on the server.

## Adding a task
1. Start from Server Task Template (Windows) or (Linux): paste its script into a new Remote Script in the CloudBolt UI, or add a Remote Script folder to the repo modeled on `plugins/OHK-lhm2916o` / `plugins/OHK-iub9a2jg` (generate the ID as AGENTS.md rule 6 says).
2. Name it, set its OS families, and declare its inputs. Supported input types: string, integer, decimal, IP, date, date and time, multi-line text, code, boolean and URL, with options or a regex pattern if you want. Read each input in the script through the `urlencode` filter as the templates do:
   - PowerShell: `$Name = [uri]::UnescapeDataString('{{ name|urlencode:"" }}')`
   - bash (inside the `CB_TASK` block): `NAME="$(urldecode '{{ name|urlencode:"" }}')"`
3. Replace the task body. Log with `Log` / `log`, fail with `throw` / `fail`, and validate every input in the script.
4. It appears in Run Task on matching servers immediately; no new action is needed.

## Notes
- Run Task never carries a password. The custom form's payload and the `task_inputs` input are written to the job log in plain text, so a script's password-type inputs are left out of the form, and a script that requires one cannot run from here. Keep secrets on the server, on the Remote Script's credentials, or in the target system.
- CloudBolt does not validate a custom-form submit, so the plug-in rechecks the task against each server and every input against the script's own definition (required, options, pattern, type) before anything runs.
- The API and MCP run the action without the form: pass `task` (the Remote Script's global ID) and `task_inputs` as a JSON object, for example `{"service_name": "Spooler", "operation": "status"}`.
- Each Remote Script keeps its own timeout, Run with sudo, Remove after run and Max retries settings. Its Commandline args are passed, but WinRM rejects arguments and Azure Run Command ignores them; inject inputs into the script body instead.
- Over WinRM a successful script's output is otherwise not shown in the job; Run Task adds it to the job progress. Azure Run Command returns only the last 4 KB of output.
- On Windows over VMware Tools any stderr text fails the script, and Write-Host output is lost; the templates write only to stdout except for one ERROR line on failure.
