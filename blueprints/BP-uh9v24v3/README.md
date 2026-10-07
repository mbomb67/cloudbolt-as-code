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
| Shared module | SHM-119fepar | ansible_job_template_linux_vm_build (example job template map) |
| Shared module | SHM-jlguerjr | tfc_api |
| Shared module | SHM-r0oq14r7 | env_options |
| Shared module | SHM-9h13o859 | vm_adoption |
| Webhook | IWH-yj93is5z | Form Options (hook OHK-fx500o2r) |
| Form | FRM-ai7lwb13 | HCP Terraform No-Code + Ansible order form |

## How the Ansible step works
- It runs after the module has applied and its VMs have been adopted as child servers of the resource (from the module's `cloudbolt_vm_ids` output). No servers means nothing to do.
- **CloudBolt never adds a server to an AAP inventory.** The job templates are expected to be bound to inventories managed outside CloudBolt; the step launches each template as-is and passes the host through the launch's limit and/or extra vars. Nothing is created in AAP, so the blueprint has no AAP teardown step.
- Which templates run, and what each receives, is declared in **job template maps**: one shared module per AAP job template. The blueprint pins the maps it uses as options of its `ansible_job_template_maps` parameter.
- For each server and each pinned map the step resolves the AAP configuration manager (the map's `manager`, else the one the server's environment maps to), finds the template by name on it, reads the template's launch metadata, renders the map, launches, and waits for the AAP job. The AAP job's status and stdout land in the CloudBolt job log.

## Job template maps
A map is a shared module whose `module_name` starts with `ansible_job_template_` and that defines a dict `JOB_TEMPLATE`. [shared_modules/SHM-119fepar](../../shared_modules/SHM-119fepar/SHM-119fepar_script.py) is the shipped example; copy it for each template and sync. Maps are reloaded on every run, so editing one needs no CloudBolt restart.

| Key | Meaning |
|---|---|
| `job_template` | Exact name of the job template in AAP. Required. |
| `manager` | AAP configuration manager name to launch through. Blank: the manager the server's environment maps to. |
| `limit` | Host pattern for the launch's limit. Blank: no limit is sent. Dropped with a warning when the template does not prompt for a limit. |
| `inventory` | Inventory name to launch against instead of the template's own. Blank: the template's inventory. Needs the template's inventory prompt on launch. |
| `scm_branch` | Branch, tag or commit for the template's project. Blank: the template's configured branch. Needs the branch prompt on launch. |
| `extra_vars` | Variable name, as the playbook or survey expects it, to value. |
| `sensitive` | Extra var keys whose values are masked in the job log. |
| `wait` | `True` (default) waits for the AAP job and fails the step when it fails; `False` launches and moves on. |

**Values are Django templates** rendered by CloudBolt's own engine (the one behind hostname templates). Non-string values (`True`, numbers, lists, dicts) are passed through unchanged; a string that renders empty (or to `None`) is left out so the template's or survey's default applies. In context:

| Variable | What it is |
|---|---|
| `server` | The child server: `server.hostname`, `server.ip`, `server.domain`, `server.cpu_cnt`, `server.mem_size`, `server.os_build` |
| `resource`, `blueprint` | The deployment and its blueprint |
| `group`, `environment`, `os_build`, `os_family` | The server's |
| `job`, `order`, `profile` | The build job, its order and the orderer |
| every parameter by name | The server's parameters, then the resource's; the resource mirrors the module's variables as `tfc_var_<name>` (for example `tfc_var_os_image`) |

Example: two templates that need the same facts under different names.

```python
# ansible_job_template_linux_vm_build
"extra_vars": {
    "vm_name": "{{ server.hostname }}{% if server.domain %}.{{ server.domain }}{% endif %}",
    "ip_address": "{{ server.ip }}",
}

# ansible_job_template_linux_vm_register
"extra_vars": {
    "survey_ip_address": "{{ server.ip }}",
    "survey_fqdn": "{{ server.hostname }}.{{ server.domain }}",
},
"limit": "{{ server.hostname }}",
```

Before launching, the step compares the rendered payload with what the template prompts for: a survey variable the template requires that renders empty fails the step with its name; a limit, inventory or branch the template does not prompt for is dropped with a warning; fields AAP reports as ignored are a warning. Credentials prompted for on launch are not supported: set them on the template.

## Prerequisites
- Everything the [HCP Terraform No-Code Module](../BP-00meiwwz/README.md#prerequisites) blueprint needs; the module must emit `cloudbolt_vm_ids`, or there are no servers to configure.
- An Ansible Automation Platform configuration manager (Admin > Configuration Managers) with a connection that may read job templates and launch them, set as the Configuration Management feature of each target environment, or named in the map's `manager`.
- The job templates, each bound to its inventory, with the variables the maps send either prompted for on launch or declared in the template's survey.

## Setup
1. Pin the four HCP Terraform coordinates exactly as for [BP-00meiwwz](../BP-00meiwwz/README.md#setup) (Blueprint > Parameters, one option each from the **Add option** dropdowns).
2. Add one job template map per template under `shared_modules/` (copy SHM-119fepar, new global ID, `module_name` starting with `ansible_job_template_`), set its `job_template` name and `extra_vars`, and sync.
3. Pin the maps as options of the `ansible_job_template_maps` parameter (destination Resource, several options allowed); **Add option** lists each synced map labeled with its job template. The form carries nothing for it. The maps run in pinned order on every server. With no options the step succeeds without doing anything.
4. Export the blueprint back to the repo (or edit `parameters[].options`) after pinning: a sync re-creates the options from the metadata.
5. Sync the repo and restart CloudBolt once so the shared modules load; re-enter the `tf-cloud` token after every sync.

## Notes
- A template missing on a server's manager is a warning and the other maps still run; a map that cannot be loaded, a required survey variable left empty, a rejected launch or a failed AAP job is a failure. With `continue_on_failure` false (as shipped) a failure fails the order and leaves the resource PROVFAILED; set it true to keep the deployment and re-run the template from AAP.
- The order form reads the blueprint's pinned parameters by the form's own IDs, so this blueprint has its own form (FRM-ai7lwb13); the field layout is the one from BP-00meiwwz. Author it for another module the same way.
- Pin a different `nocode-*` module here than on BP-00meiwwz, or disable discovery on one of them: both discovery plugins claim every workspace of their pinned module.
- Day-2 runs (Update Variables, Deploy Latest Version) do not re-run the job templates; a replaced VM gets a new server record but no Ansible run. Run the template from AAP or order again.
