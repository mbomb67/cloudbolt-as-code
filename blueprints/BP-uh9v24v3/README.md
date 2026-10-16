# HCP Terraform No-Code + Ansible

The [HCP Terraform No-Code Module](../BP-00meiwwz/README.md) blueprint plus a second build step that runs Ansible Automation Platform (AAP) job templates on the deployment's servers. The HCP Terraform half is identical (same plugins, form layout, discovery, day-2 actions and resource type); read that README for it. This one covers the Ansible step.

Deployed resources are of type **HCP Terraform Workspace**.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-axtt0yqq | HCP Terraform No-Code Module |
| Build (step 2) | OHK-cwqouaqn | Run Ansible Job Templates on Servers |
| Teardown | OHK-y9d1uwhw | Teardown HCP Terraform No-Code Module |
| Discovery | OHK-b1n02ula | Discover HCP Terraform No-Code Workspaces |
| Options hook | HPA-swe1kifa | Generate options for HCP Terraform coordinates (hook OHK-529jjzli) |
| Options hook | HPA-aualk6ei | Generate options for Ansible job template maps (hook OHK-7i1fk0l5) |
| Day-2 action | RSA-dxrh4m6j | Update Variables (hook OHK-lvy5tj0y, form FRM-h4py5w3a) |
| Day-2 action | RSA-pngq92ss | Deploy Latest Version (hook OHK-4y8f1vff) |
| Shared module | SHM-jlguerjr | tfc_api |
| Shared module | SHM-r0oq14r7 | env_options |
| Shared module | SHM-9h13o859 | vm_adoption |
| Webhook | IWH-yj93is5z | Form Options (hook OHK-fx500o2r) |
| Form | FRM-ai7lwb13 | HCP Terraform No-Code + Ansible order form |
| Example | [docs/examples/ansible/linux-vm-build.variable-map.json](../../docs/examples/ansible/linux-vm-build.variable-map.json) | Job template map for a Linux VM build template |

## How the Ansible step works
- It runs after the module has applied and its VMs have been adopted as child servers of the resource (from the module's `cloudbolt_vm_ids` output). No servers means nothing to do.
- **CloudBolt never adds a server to an AAP inventory.** The job templates are expected to be bound to inventories managed outside CloudBolt; the step launches each template as-is and passes the host through the launch's limit and/or extra vars. Nothing is created in AAP, so the blueprint has no AAP teardown step.
- Which templates run, and what each receives, is declared in **job template maps**: one CloudBolt Variable Map per AAP job template. The blueprint pins the maps it uses as options of its `ansible_job_template_maps` parameter.
- For each server and each pinned map the step resolves the AAP configuration manager from the server's environment (its Configuration Management feature), finds the template by name on it, reads the template's launch metadata, renders the map, launches, and waits for the AAP job. The AAP job's status and stdout land in the CloudBolt job log.

## Job template maps
A map is a **Variable Map** (Admin > Variable Maps) whose JSON has a `job_template` key. Variable Maps are CloudBolt's templated-JSON configuration object, the same one Terraform Operation items use, edited in a JSON editor with pickers for `group`, `environment`, `job`, `server` and `resource`. The step reads the map from the database on every run, so an edit takes effect on the next order. Configuration, not code: no Python, no restart.

Variable Maps are not a Source Code Repos content type, so they do not sync from the repo. Create each one on the instance from its JSON, either pasted into Admin > Variable Maps > Add (the `map` object) or posted whole to the API:

```bash
curl -sS -X POST https://<cloudbolt>/api/v3/cmp/variableMaps/ -H "Authorization: Bearer <token>" -H "Content-Type: application/json" --data @docs/examples/ansible/linux-vm-build.variable-map.json
```

Keep the JSON files in the repo (`docs/examples/ansible/`) as the source of truth and re-post after editing.

| Key | Meaning |
|---|---|
| `job_template` | Exact name of the job template in AAP. Required; a Variable Map without it is not listed as a job template map. |
| `limit` | Host pattern for the launch's limit. Blank: no limit is sent. Dropped with a warning when the template does not prompt for a limit. |
| `inventory` | Inventory name to launch against instead of the template's own. Blank: the template's inventory. Needs the template's inventory prompt on launch. |
| `scm_branch` | Branch, tag or commit for the template's project. Blank: the template's configured branch. Needs the branch prompt on launch. |
| `extra_vars` | Variable name, as the playbook or survey expects it, to value. |
| `sensitive` | Extra var keys whose values are masked in the job log. |
| `wait` | `true` (default) waits for the AAP job and fails the step when it fails; `false` launches and moves on. |

**Values are Django templates** rendered by CloudBolt's own engine (the one behind hostname templates). Non-string values (`true`, numbers, lists, objects) are passed through unchanged; a string that renders empty (or to `None`) is left out so the template's or survey's default applies. In context:

| Variable | What it is |
|---|---|
| `server` | The child server: `server.hostname`, `server.ip`, `server.domain`, `server.cpu_cnt`, `server.mem_size`, `server.os_build` |
| `resource`, `blueprint` | The deployment and its blueprint |
| `group`, `environment`, `os_build`, `os_family` | The server's |
| `job`, `order`, `profile` | The build job, its order and the orderer |
| every parameter by name | The server's parameters, then the resource's; the resource mirrors the module's variables as `tfc_var_<name>` (for example `tfc_var_os_image`) |

Example: two templates that need the same facts under different names.

```json
// Variable Map "Ansible: Linux VM Build"
"extra_vars": {
    "vm_name": "{{ server.hostname }}{% if server.domain %}.{{ server.domain }}{% endif %}",
    "ip_address": "{{ server.ip }}"
}

// Variable Map "Ansible: Linux VM Register"
"extra_vars": {
    "survey_ip_address": "{{ server.ip }}",
    "survey_fqdn": "{{ server.hostname }}.{{ server.domain }}"
},
"limit": "{{ server.hostname }}"
```

Before launching, the step compares the rendered payload with what the template prompts for: a survey variable the template requires that renders empty fails the step with its name; a limit, inventory or branch the template does not prompt for is dropped with a warning; fields AAP reports as ignored are a warning. Credentials prompted for on launch are not supported: set them on the template.

## Prerequisites
- Everything the [HCP Terraform No-Code Module](../BP-00meiwwz/README.md#prerequisites) blueprint needs; the module must emit `cloudbolt_vm_ids`, or there are no servers to configure.
- An Ansible Automation Platform configuration manager (Admin > Configuration Managers) with a connection that may read job templates and launch them, set as the Configuration Management feature of each environment you order into; the server's environment is what selects the manager.
- The job templates, each bound to its inventory, with the variables the maps send either prompted for on launch or declared in the template's survey.

## Setup
1. Pin the four HCP Terraform coordinates exactly as for [BP-00meiwwz](../BP-00meiwwz/README.md#setup) (Blueprint > Parameters, one option each from the **Add option** dropdowns).
2. Create one Variable Map per job template from a JSON file under `docs/examples/ansible/` (copy the example, set `job_template` and `extra_vars`), in Admin > Variable Maps or through the API as above.
3. Pin the maps as options of the `ansible_job_template_maps` parameter (destination Resource, several options allowed); **Add option** lists each job template map labeled with its template, name and `MAP-` ID, and stores the ID. The form carries nothing for it. The maps run in pinned order on every server. With no options the step succeeds without doing anything.
4. Export the blueprint back to the repo (or edit `parameters[].options`) after pinning: a sync re-creates the options from the metadata. A pinned `MAP-` ID is instance-specific, like a ConnectionInfo ID; the parameter also accepts the map's exact name, which travels between instances.
5. Sync the repo and restart CloudBolt once so the shared modules load; re-enter the `tf-cloud` token after every sync.

## Notes
- A template missing on a server's manager is a warning and the other maps still run; a map that cannot be found or has an unknown key (`manager` is not one), a required survey variable left empty, a rejected launch or a failed AAP job is a failure. With `continue_on_failure` false (as shipped) a failure fails the order and leaves the resource PROVFAILED; set it true to keep the deployment and re-run the template from AAP.
- The order form reads the blueprint's pinned parameters by the form's own IDs, so this blueprint has its own form (FRM-ai7lwb13); the field layout is the one from BP-00meiwwz. Author it for another module the same way.
- Pin a different `nocode-*` module here than on BP-00meiwwz, or disable discovery on one of them: both discovery plugins claim every workspace of their pinned module.
- Day-2 runs (Update Variables, Deploy Latest Version) do not re-run the job templates; a replaced VM gets a new server record but no Ansible run. Run the template from AAP or order again.
