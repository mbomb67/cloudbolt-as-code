"""
This is a working sample CloudBolt plug-in for you to start with. The run method is required,
but you can change all the code within it. See the "CloudBolt Plug-ins" section of the docs for
more info and the CloudBolt forge for more examples:
https://github.com/CloudBoltSoftware/cloudbolt-forge/tree/master/actions/cloudbolt_plugins
"""
from common.methods import set_progress


def run(job, resource=None, *args, **kwargs):
    port_field_name = "{{ port_field_name }}"
    site_port = resource.get_value_for_custom_field(port_field_name)
    server = resource.server_set.first()
    url = f"http://{server.ip}:{site_port}"
    resource.set_value_for_custom_field("site_url", url)
    return "SUCCESS", "", ""