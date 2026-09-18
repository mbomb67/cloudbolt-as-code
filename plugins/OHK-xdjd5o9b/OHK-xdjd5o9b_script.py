import json

from common.methods import generate_string_from_template, set_progress
from connectors.ansible.models import AnsibleConf
from utilities.logger import ThreadLogger
from infrastructure.models import CustomField
from c2_wrapper import create_custom_field


logger = ThreadLogger(__name__)

ANSIBLE_ID = "{{ansible_manager}}"
PLAYBOOK_PATH = "{{playbook_path}}"
LIMIT = ""

# Prefix used to namespace Ansible-owned CustomFields on the Resource.
# Drives two behaviors:
#   1) Stats captured from the playbook are stored as `<ANSIBLE_PREFIX><key>`.
#   2) When passing the Resource's CFs back to a playbook as extra-vars, any
#      key that starts with this prefix has the prefix stripped — so the same
#      plugin can power build and teardown. Build stores `ansible_vpc_id`;
#      teardown reads it and passes it back to the playbook as `vpc_id`.
ANSIBLE_PREFIX = "ansible_"


def generate_options_for_ansible_manager(**kwargs):
    hosts = AnsibleConf.objects.all()
    options = [(host.id, host.name) for host in hosts]
    if not options:
        options = [('', '--- First create a Configuration Manager ---')]
    return options


def run(job, **kwargs):
    """
    A CloudBolt Plugin that will execute an Ad Hoc Ansible Playbook.
    This will read all Parameters set on the resource and pass them in as Extra
    Vars to the Playbook. Hosts and Limit options are optional.

    The playbook is run with the JSON stdout callback (and
    ANSIBLE_SHOW_CUSTOM_STATS so set_stats values appear) so we can parse the
    `stats` section directly from stdout and persist each stat as a
    CustomField on the Resource.
    """
    logger.debug(f'kwargs: {kwargs}')
    resource = kwargs.get('resource')
    if not ANSIBLE_ID or "FILL-ME" in ANSIBLE_ID:
        return "FAILURE", "", "ansible_manager is not set. Edit the blueprint parameter_defaults (see README)."
    ansible = AnsibleConf.objects.get(id=ANSIBLE_ID)
    extra_vars = build_extra_vars(resource)

    script_contents = generate_playbook_command(
        PLAYBOOK_PATH, LIMIT, ansible, extra_vars, job, resource
    )
    output = ansible.connection_info.execute_script(
        script_contents=script_contents, timeout=600
    )

    stats = parse_ansible_stats(output)
    if stats and resource:
        set_progress(f"Capturing {len(stats)} Ansible stat(s) onto Resource")
        write_stats_to_resource(stats, resource)

    return "SUCCESS", output, ""


def build_extra_vars(resource):
    """
    Build the extra-vars JSON passed to ansible-playbook from the Resource's
    CustomFieldValues. Any CF whose name starts with ANSIBLE_PREFIX has the
    prefix stripped so playbooks see plain variable names (e.g. CF
    `ansible_vpc_id` is passed to the playbook as `vpc_id`). Non-prefixed CFs
    pass through unchanged.

    Values previously stored by `write_stats_to_resource` as JSON-encoded
    strings (lists / dicts that came back from `set_stats`) are decoded back
    to their native types here so the playbook receives a real list or dict,
    not a quoted string that would fail Jinja's `loop` / map filters.
    """
    extra_vars = {}
    for key, value in resource.get_cf_values_as_dict().items():
        if key.startswith(ANSIBLE_PREFIX):
            key = key[len(ANSIBLE_PREFIX):]
        extra_vars[key] = _decode_if_json(value)
    rendered = json.dumps(extra_vars)
    logger.debug(f"Ansible extra_vars: {rendered}")
    return rendered


def _decode_if_json(value):
    """
    Best-effort decode of JSON-encoded list/dict values that were previously
    serialized into a STR CF, so the playbook sees the native structure.
    Leaves plain strings, ints, and already-native lists/dicts alone.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "[{":
            try:
                return json.loads(stripped)
            except (ValueError, TypeError):
                pass
    return value


def generate_playbook_command(playbook_path, limit, ansible, extra_vars,
                              job, resource):
    # inventory_path = ansible.inventory_path
    # ANSIBLE_STDOUT_CALLBACK=json makes ansible-playbook emit a single JSON
    # document on stdout containing per-host `stats`. ANSIBLE_SHOW_CUSTOM_STATS
    # is required for custom stats set via the `set_stats` module to actually
    # appear in the output (`global_custom_stats` / per-host `custom_stats`).
    cmd = (
        f"ANSIBLE_STDOUT_CALLBACK=json ANSIBLE_SHOW_CUSTOM_STATS=true "
        f"ansible-playbook {playbook_path}" # -i {inventory_path}"
    )
    if limit:
        cmd += f' --limit "{limit}"'

    if extra_vars:
        # Extra Vars could use Django Templating to reference things we know
        group = job.get_resource().group
        local_context = {"resource": resource}
        extra_vars = generate_string_from_template(
            template=extra_vars,
            group=group,
            env=None,
            os_build=None,
            context=local_context
        )
        cmd += f" --extra-vars='{extra_vars}'"

    logger.debug(f"Generated Ansible playbook command: {cmd}")
    return cmd


def parse_ansible_stats(raw):
    """
    Parse the JSON callback output and return a flat dict of user-authored
    stats from the playbook's `set_stats` calls. Ignores the top-level
    `stats` block (play-recap counters: ok/changed/failures/...) since those
    are execution metadata, not metadata the user wants surfaced on the
    Resource.

    Sources:
      - `global_custom_stats`: from `set_stats` with `per_host: false`
        (aggregate metadata — the typical case for things like
        aws_account_id, resource_url, etc.).
      - `custom_stats`: from `set_stats` with `per_host: true`; flattened
        as `<host>_<key>`.
    """
    if not raw:
        return {}
    # Locate the JSON object inside the captured output in case any non-JSON
    # text (shell init noise, ansible warnings) is present around it.
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        logger.warning("Could not parse Ansible JSON output for stats capture")
        return {}

    stats = {}

    global_custom_stats = data.get("global_custom_stats") or {}
    if isinstance(global_custom_stats, dict):
        stats.update(global_custom_stats)

    custom_stats = data.get("custom_stats") or {}
    if isinstance(custom_stats, dict):
        for host, host_stats in custom_stats.items():
            if not isinstance(host_stats, dict):
                continue
            for key, value in host_stats.items():
                stats[f"{host}_{key}"] = value

    return stats


def write_stats_to_resource(stats, resource):
    """
    Persist each Ansible stat as a CustomField on the Resource.

    Lists and dicts are JSON-encoded into a single STR CFV. CloudBolt's
    `set_value_for_custom_field` does not natively round-trip a Python list
    onto a multi-value CF, so storing the JSON representation and decoding
    it again in `build_extra_vars` is simpler and works in both directions.
    """
    logger.info(f"Writing Ansible stats to Resource {resource.id}: {stats}")
    for key, value in stats.items():
        cf_name = f"{ANSIBLE_PREFIX}{key}"
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        defaults = {
            "label": key.replace("_", " ").title(),
            "description": "Created by Ansible Playbook stats",
            "show_on_servers": True,
            "type": "STR",
        }
        CustomField.objects.get_or_create(name=cf_name, defaults=defaults)
        resource.set_value_for_custom_field(cf_name, value)
