"""
Ansible job template map: Linux VM Build.

One map describes one Ansible Automation Platform job template. The "Run
Ansible Job Templates on Servers" build step (plugins/OHK-cwqouaqn) loads the
maps a blueprint pins on its ansible_job_template_maps parameter and, for
every server the deployment owns, launches the template named here with the
payload built below. CloudBolt never adds the server to an inventory: the
template runs against its own inventory and learns about the host from the
limit and/or the extra vars declared here.

To describe another template, copy this shared module: the module_name must
start with ``ansible_job_template_`` (lowercase letters and underscores), and
the module must define JOB_TEMPLATE. Each template declares its own variable
names, so one map can send the IP as ``ip_address`` and another as
``survey_ip_address``, both from the same server fact.

String values are Django templates rendered by CloudBolt's own engine (the one
behind hostname templates). In context:
  server, resource, blueprint, group, environment, os_build, os_family, job,
  order, profile (the orderer), portal, and every parameter of the server and
  of the resource by name (the resource mirrors the module's variables as
  tfc_var_<name>; the server's parameters win on a name clash).
A string that renders empty (or to Python's None) is left out of the launch,
so the template's or the survey's default applies; a required survey variable
left empty fails the step before the launch. Non-string values (bool, int,
list, dict) are passed through unchanged. The ``str_tags`` template library is
loaded.
"""

JOB_TEMPLATE = {
    # Exact name of the job template in AAP. Required.
    "job_template": "Linux VM Build",
    # Name of the AAP configuration manager (Admin > Configuration Managers)
    # to launch through. Blank: the manager the server's environment maps to.
    "manager": "",
    # Host pattern for the launch's limit. Blank: no limit is sent. This
    # template's inventory is managed outside CloudBolt and its playbook
    # targets the host by vm_name, so none is needed. A limit is honored only
    # when the template prompts for one on launch; otherwise it is dropped
    # with a warning.
    "limit": "",
    # Inventory name to launch against instead of the template's own. Blank:
    # the template's inventory. Needs "prompt on launch" for the inventory.
    "inventory": "",
    # Branch, tag or commit of the template's project. Blank: the template's
    # configured branch. Needs "prompt on launch" for the branch.
    "scm_branch": "",
    # Extra vars: variable name as the playbook or survey expects it -> value.
    "extra_vars": {
        "vm_name": "{{ server.hostname }}{% if server.domain %}.{{ server.domain }}{% endif %}",
        "ip_address": "{{ server.ip }}",
        "application": "{{ group.name }}",
        "sdlc_environment": "{{ environment.name }}",
        "os_image": "{{ tfc_var_os_image }}",
        "requested_by": "{{ profile.user.username }}",
        "config_managed": "yes",
        "create_snow_incidents": False,
    },
    # Extra var keys whose values must not appear in the CloudBolt job log.
    "sensitive": [],
    # Wait for the AAP job and fail the step when it fails. False: launch and
    # move on.
    "wait": True,
}
