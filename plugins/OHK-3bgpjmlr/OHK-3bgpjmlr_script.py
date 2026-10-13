"""
A CloudBolt Build Plugin to run an Aria Orchestration workflow from a CloudBolt
action. This plugin will run the workflow and set the outputs of the workflow
as custom fields on the resource.

Prerequisites:
    - Configure a Connection Info object for Aria Automation in CloudBolt with
    "vra8" as a label
    - CloudBolt must be at least on version 2023.5.1

The plugin requires the following parameters:
    - vra_connection: The Aria Orchestrator connection to use
    - workflow_id: The id of the workflow to run
    - workflow_parameters: A dict of the parameters to pass to the workflow
    - flatten_params: A boolean to determine if the sdk-object outputs should
        be flattened to the resource or written to a json string

CloudBolt Custom Forms can be used to feed the value for the workflow_parameters
parameter. The Dynamic Panel can be used to create a list of dicts for the
workflow_parameters parameter. The dict should be of the form:
    {
        "key": "value",
        "key2": "value2"
    }

The values in the dict will be rendered through the Django template engine. This
allows you to reference objects set on the resource, group, or job. For example,
if the user wants to pass the value of the resource's owner to the workflow,
they can use the following:
    {
        "name": "{{resource.owner}}"
    }

For sdk-object outputs, the plugin will create 3 custom fields on the resource:
    - vro_{workflow_id}_{output_name}: A custom field that will contain the
        sdk-object as a json string
    - vro_{workflow_id}_{output_name}_vro_type: A custom field that will
        contain the sdk-object's type
    - vro_{workflow_id}_{output_name}_attrs: A custom field that will contain
        the sdk-object's attributes as a json string

The plugin will also create a custom field for each attribute on the sdk-object
if the flatten_params parameter is set to True. The custom field name will be
of the form:
    vro_{workflow_id}_{output_name}_{attribute_name}

If using this plugin to delete a resource, the following parameters can be
set on the workflow to delete a sdk-object (VC:VM, Custom Resource, basically
anything that isn't a string, number, or boolean):
- teardown_sdk_{resource_name}: A dict of the form:
    {
        "name": "name of the workflow input parameter",
        "type": "the name of the workflow input parameter that contains the
            type of the sdk-object",
        "value": "the name of the workflow input parameter that contains
            the sdk-object",
        "scope": "scope of the sdk-object"
    }
The name, type, and value are required. The scope is optional and will
default to local if not provided. The name and type should be set to
the name and type of the sdk-object that is returned from the workflow.
Following an example, the value and type reference the names of the parameters
on the resource where the Aria Orchestrator type and value are stored. This
follows the format of vro_{workflow_id}_{output_name}_vro_type and
vro_{workflow_id}_{output_name} respectively:
"teardown_sdk_object_VcVm": {
    "value": "vro_0e0672f5-9632-4ba0-af3a-b52ac24f9d8f_output_vc_vm",
    "type": "vro_0e0672f5-9632-4ba0-af3a-b52ac24f9d8f_output_vc_vm_vro_type",
    "name": "myVcVm",
    "scope": "local"
}

"""
import html

import requests
import yaml
import time
import json
import re
from ast import literal_eval
from urllib.parse import urlencode
from django.template import Template, Context
from c2_wrapper import create_custom_field
from common.methods import set_progress
from utilities.helpers import get_ssl_verification
from utilities.models import ConnectionInfo
from shared_modules.aria_connection import (
    AriaOrchestratorConnection, generate_options_for_aria_connection
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)



def generate_options_for_workflow_id(field, control_value=None, **kwargs):
    logger.debug(f'kwargs: {kwargs}')
    if not kwargs.get("group", None):
        return None
    if not control_value:
        return [("", "------Select a vRO connection first------")]
    vro = AriaOrchestratorConnection(control_value)
    return vro.generate_options_for_workflow_id()


def run(job, resource=None, **kwargs):
    logger.debug(f'kwargs: {kwargs}')
    vro_id = "{{aria_connection}}"
    workflow_id = "{{workflow_id}}"
    # flatten_params allows the user to specify if any attributes found on a
    # sdk-object output should be flattened to the resource. For example, if
    # the workflow returns a VM as an output, the user can choose to have the
    # VM's attributes set on the resource.
    # True will set the VM's attributes on the resource, False will write all
    # params to a json string.
    flatten_params = literal_eval("{{flatten_params}}")
    # The Dynamic Panel in a custom form will return a list of dicts.
    workflow_parameters = """{{workflow_parameters}}"""
    logger.debug(f'workflow_parameters: {workflow_parameters}')
    # Take the dict string of workflow parameters and convert it to a dict after
    # rendering the template values
    params = literal_eval(render_values(workflow_parameters, resource, job))
    if type(params) is list:
        if len(params) > 1:
            raise Exception(f"Params should be a dict. Got: {params}")
        params = params[0]
    if not params:
        params = {}
    if type(params) is not dict:
        raise Exception(f"Params should be a dict. Got: {type(params)}")
    if job.top_level_job.title.startswith("Delete Resource"):
        params = create_delete_params(params, resource)
    vro = AriaOrchestratorConnection(vro_id)
    set_progress(f'Running workflow {workflow_id}, params: {params}, '
                 f'flatten_params: {flatten_params}')
    workflow_execution = run_workflow(vro, workflow_id, params)
    if resource:
        # If there is no resource this BP was set to a resource type of None
        # No resource is passed in
        write_outputs_to_resource(workflow_execution, resource, vro,
                                  flatten_params, workflow_id)
    return "SUCCESS", "", ""


def run_workflow(vro, workflow_id, params_dict):
    workflow_execution = vro.execute_workflow(workflow_id, params_dict)
    execution_id = workflow_execution["id"]
    status = vro.wait_for_workflow_execution(workflow_id, execution_id)
    logger.info(f"Workflow execution completed. Status: {status}")
    if status == "failed" or status == "canceled":
        raise Exception(f"Workflow execution failed. Status: {status}")
    workflow_execution = vro.get_workflow_execution(workflow_id, execution_id)
    return workflow_execution


def write_outputs_to_resource(workflow_execution, resource, vro, flatten_params,
                              workflow_id, ignore_prefixes=None):
    if ignore_prefixes is None:
        ignore_prefixes = ["__"]
    outputs = workflow_execution["output-parameters"]
    standard_types = ["string", "number", "boolean"]
    workflow_name = workflow_execution["name"]
    for output in outputs:
        logger.debug(f"Processing output: {output}")
        if output["name"].startswith(tuple(ignore_prefixes)):
            logger.debug(f"Skipping output {output['name']}")
            continue
        try:
            value_type = list(output["value"].keys())[0]
        except KeyError:
            logger.warning(f"Skipping output {output['name']}. No value found")
            continue

        output_name = output["name"]
        output_type = output["type"]
        if value_type == "sdk-object":
            write_sdk_object_to_resource(vro, resource, output, value_type,
                                         output_name, flatten_params,
                                         workflow_id)
        elif output_type in standard_types:
            cb_type = get_cb_type_from_vro_type(output_type)
            value = output["value"][value_type]["value"]
            create_cf_set_value(resource, output_name, value, workflow_id,
                                cb_type)
        elif output_type.startswith("Array/"):
            # This is a list of dicts
            sub_type = output["type"].split("/")[-1]
            cb_type = get_cb_type_from_vro_type(sub_type)
            values = output["value"]["array"]["elements"]
            # value input will accept a list of the values
            value = [v[sub_type]["value"] for v in values]
            create_cf_set_value(resource, output_name, value, workflow_id,
                                cb_type)
        else:
            logger.warning(f"Skipping output {output_name}. Type: "
                           f"{output_type} is not supported")
            continue


def write_sdk_object_to_resource(vro, resource, output, value_type, output_name,
                                 flatten_params, workflow_id):
    # We want to write 3 different types of things for a sdk-object
    # 1. The sdk-object itself will be written as a json string to a
    # custom field titled vro_{output_name}
    # 2. The sdk_object's type will be written as a string - enabling us
    # to reconstruct the vRO payload for destroy. This will be written
    # to a custom field titled vro_{output_name}_vro_type
    # 3. The attributes of the sdk-object
    href = output["value"][value_type]["href"]

    # Create the custom field for the sdk-object
    output_value = json.dumps(output["value"])
    create_cf_set_value(resource, output_name, output_value, workflow_id,
                        "TXT")

    # Create the custom field for the sdk-object's type
    output_type = output["type"]
    output_type_name = f'{output_name}_vro_type'
    create_cf_set_value(resource, output_type_name, output_type, workflow_id,
                        "STR")

    sdk_attrs = get_sdk_attrs(vro, href)
    if flatten_params:
        for key, value in sdk_attrs.items():
            key_name = f"{output_name}_{key}"
            # All Attrs are strings on a sdk-object
            create_cf_set_value(resource, key_name, value, workflow_id, "STR")
    else:
        value = json.dumps(sdk_attrs)
        attrs_name = f'{output_name}_attrs'
        # Setting to TXT because json strings can be lengthy
        create_cf_set_value(resource, attrs_name, value, workflow_id, "TXT")


def get_cb_type_from_vro_type(vro_type):
    if vro_type == "string":
        return "STR"
    if vro_type == "number":
        return "INT"
    if vro_type == "boolean":
        return "BOOL"
    return "STR"


def create_cf_set_value(resource, output_name, value, workflow_id,
                        cf_type="STR"):
    # To keep parameter names Namespaced to the vRO workflow and prevent
    # conflicting with other vRO params, going to use the workflow id as a
    # prefix
    cf_name = camel_to_snake(f'vro_{workflow_id}_{output_name}')
    cf_label = get_label(output_name)
    logger.debug(f"Creating custom field for output: {cf_name}, label: "
                 f"{cf_label} value: {value}, type: {cf_type}")
    cf = create_custom_field(cf_name, cf_label, cf_type,
                             description="Created by the vRO CloudBolt Plugin",
                             show_as_attribute=True)
    resource.set_value_for_custom_field(cf.name, value)


def get_sdk_attrs(vro, href):
    """
    Get the attributes of an SDK object. Returns a dict of the form
    """
    response = requests.get(href, headers=vro.headers,
                            verify=get_ssl_verification())
    try:
        response.raise_for_status()
    except Exception as e:
        logger.error(f'Error encountered for URL: {href}, details: '
                     f'{e.response.content}')
    attrs = {}
    for attr in response.json()["attributes"]:
        attrs[attr["name"]] = attr["value"]
    return attrs


def get_label(name):
    # Get string in snake case first - vRO uses Camel case often. Also, limit
    # to 50 chars per DB constraints on the label param
    snake_name = camel_to_snake(name)
    return snake_name.replace("_", " ").title()[:50]


def camel_to_snake(name):
    name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', name).lower()


def render_values(value, resource, job):
    """
    Renders the values in the values dict using the resource and job. The
    values dict should be a dict of the form:
    {
        "key": "value",
        "key2": "value2"
    }
    """
    context = {
        "job": job,
    }
    if resource:
        context["resource"] = resource
        context["group"] = resource.group

    context = Context(context)
    rendered_value = None
    was_dict = False
    if type(value) is dict:
        value = json.dumps(value)
        was_dict = True
    if type(value) is str:
        if value.find('{{') > -1 or value.find('{%') > -1:
            template = Template(value)
            # Hit some instances where strings were rendering with unicode
            # hex html.unescape fixes this
            rendered_value = html.unescape(template.render(context))
            if rendered_value != value:
                logger.debug(f'Rendered value: {value} to '
                             f'rendered_value: {rendered_value}')
    if not rendered_value:
        rendered_value = value
    if was_dict:
        rendered_value = json.loads(rendered_value)
    return rendered_value


def create_delete_params(params, resource):
    delete_params = {}
    for key, value in params.items():
        if type(value) is dict and key.startswith("teardown_sdk_"):
            delete_object = {
                "value": json.loads(
                    resource.get_cfv_for_custom_field(value["value"]).value
                ),
                "type": resource.get_cfv_for_custom_field(value["type"]).value,
                "name": value["name"],
                "scope": value.get("scope", "local")
            }
            delete_params[key] = delete_object
        else:
            delete_params[key] = value
    logger.debug(f"delete_params: {delete_params}")
    return delete_params
