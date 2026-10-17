# Run Task

One action for every Remote Script that fits a VM. Its form lists the Remote Scripts that can run on the selected VM, shows the chosen script's own inputs, and runs it there with the script's own transport, timeout and credentials. New tasks are new Remote Scripts, not new buttons.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-o4tgygjs | Run Task |
| Custom form | FRM-a5edrdj4 | Run Task |
| Form function | FJS-m3awusu7 | runbookTaskForm |
| Shared module | SHM-571l7249 | runbook_tasks |
| Inbound webhook | IWH-cdf543g3 | Runbook Tasks (`runbook-tasks`), called by the form |
| Remote Script | OHK-lhm2916o | Runbook Task Template (Windows) |
| Remote Script | OHK-iub9a2jg | Runbook Task Template (Linux) |
| Remote Script | OHK-01kmavro | Manage Windows Service |
| Remote Script | OHK-5yh12jiq | Manage Linux Service |

## Setup
1. Sync the Runbooks package (`packages/PKG-v20qv308`), or sync `server_actions/`, `webhooks/` and the four Remote Scripts. Syncing the action alone does not bring the webhook or the Remote Scripts.
2. Restart the CloudBolt web service after the first sync and after any change to `runbook_tasks`: the form's webhook and the task list run in the web process, which keeps the old shared module until it restarts.
3. Give the groups that should run tasks permission to run this action.

## Which tasks a VM lists
Every Remote Script that passes the checks CloudBolt makes before it runs a Remote Script on a VM:
- no Run on Server set;
- the VM's resource handler can run scripts;
- the script's OS families, resource technologies, environments, groups and configuration managers are empty or include the VM's (an OS family includes its children, so Windows covers Windows 2022).

A bulk run lists only the tasks that fit every selected VM. Build-step scripts are listed too; restrict a script with its OS families, groups or environments to keep it off VMs.

## Credentials
The task uses the Remote Script's saved credentials, else the VM's. Azure and AWS VMs with tech-specific script execution (Run Command, SSM) need none. When CloudBolt has no way to log in, the form shows a username and password box that saves them on the VM, encrypted, as CloudBolt's own credential prompt does. Saving needs the Manage Credentials permission on the VM.

## Adding a task
1. Start from Runbook Task Template (Windows) or (Linux). CloudBolt's UI creates a Remote Script only inside a context (a server, resource or orchestration action), so use one of these:
   - on the **Actions** tab of Admin > Custom Extensions > Runbooks ([XUI-jbm5ac9e](../../extensions/XUI-jbm5ac9e/)), choose New Remote Script: it builds the script from the template and does steps 2 and 3 for you, with your inputs and task body;
   - add a Remote Script folder to the repo modeled on `plugins/OHK-lhm2916o` / `plugins/OHK-iub9a2jg` (generate the ID as AGENTS.md rule 6 says) and sync `plugins/OHK-<id>` on its own, through the Source Control Repos API or the Runbooks package;
   - in the UI, add it as a new Server Action with a Remote Script, then disable that server action: its button disappears and Run Task still lists the script. Deleting the server action instead would delete the script with it.
2. Name it, set its OS families, and declare its inputs. Supported input types: string, integer, decimal, IP, date, date and time, multi-line text, code, boolean and URL, with options or a regex pattern if you want. Read each input percent-encoded, as the templates do, so no value can break the script's quoting:
   - PowerShell: `$Name = [uri]::UnescapeDataString('{% autoescape off %}{% filter urlencode:"" %}{{ name }}{% endfilter %}{% endautoescape %}')`
   - bash (inside the `CB_TASK` block): `NAME="$(urldecode '{% autoescape off %}{% filter urlencode:"" %}{{ name }}{% endfilter %}{% endautoescape %}')"`

   Keep `{{ name }}` bare, never `{{ name|urlencode }}`: CloudBolt rebuilds a script's inputs from the bare template variables in it on every save, so a filtered variable becomes a stray input and the declared one is deleted.
3. Replace the task body. Log with `Log` / `log`, fail with `throw` / `fail`, and validate every input in the script. To publish values, call `Write-TaskOutput name value` / `task_output name value` last: each prints a `CB_OUTPUT name=value` line, which Run Task records as the job's outputs and a later workflow step can read.
4. It appears in Run Task on matching VMs immediately; no new action is needed.

## Notes
- Run Task never carries a password. The custom form's payload and the `task_inputs` input are written to the job log in plain text, so a script's password-type inputs are left out of the form, and a script that requires one cannot run from here. Keep secrets on the VM, on the Remote Script's credentials, or in the target system.
- CloudBolt does not validate a custom-form submit, so the plug-in rechecks the task against each VM and every input against the script's own definition (required, options, pattern, type) before anything runs.
- The API and MCP run the action without the form: pass `task` (the Remote Script's global ID) and `task_inputs` as a JSON object, for example `{"service_name": "Spooler", "operation": "status"}`.
- Each Remote Script keeps its own timeout, Run with sudo, Remove after run and Max retries settings. Its Commandline args are passed, but WinRM rejects arguments and Azure Run Command ignores them; inject inputs into the script body instead.
- Over WinRM a successful script's output is otherwise not shown in the job; Run Task adds it to the job progress. Azure Run Command returns only the last 4 KB of output, and runs one script per VM at a time: a task started while another runs waits up to five minutes for it.
- On Windows over VMware Tools any stderr text fails the script, and Write-Host output is lost; the templates write only to stdout except for one ERROR line on failure.
- A task can name files from the File Archives tab of the Runbooks page (`extensions/XUI-jbm5ac9e`) in `# cb-archive: archive/file` header lines. Run Task downloads them to the VM, checks them and puts them in `CB_ARCHIVE_DIR` before the task body runs; see that extension's README.
