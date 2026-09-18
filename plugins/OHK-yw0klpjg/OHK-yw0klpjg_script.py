"""
This is a working sample CloudBolt plug-in for you to start with. The run method is required,
but you can change all the code within it. See the "CloudBolt Plug-ins" section of the docs for
more info and the CloudBolt forge for more examples:
https://github.com/CloudBoltSoftware/cloudbolt-forge/tree/master/actions/cloudbolt_plugins
"""
from common.methods import set_progress


def run(job, resource=None, *args, **kwargs):
    resource_name_field = "{{ resource_name_field }}"
    resource_name = resource.get_cfv_for_custom_field(resource_name_field).value
    server_name = resource.server_set.first().hostname
    new_resource_name = f'{resource_name} ({server_name})'
    set_progress(f'resource name being set to {new_resource_name}')
    resource.name = new_resource_name
    resource.save()
    return "SUCCESS", "", ""