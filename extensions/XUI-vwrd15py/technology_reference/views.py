"""Technology Reference tab on Resource Handler and Environment pages, plus its .xlsx download."""

from django.http import HttpResponse
from django.shortcuts import render

from extensions.views import tab_extension, TabExtensionDelegate
from infrastructure.models import Environment
from resourcehandlers.models import ResourceHandler

from xui.technology_reference.utilities import TechnologyReference


class HandlerDelegate(TabExtensionDelegate):
    def should_display(self):
        return getattr(self.instance, "resource_technology_id", None) is not None


class EnvironmentDelegate(TabExtensionDelegate):
    def should_display(self):
        handler = getattr(self.instance, "resource_handler", None)
        return handler is not None and handler.resource_technology_id is not None


@tab_extension(model=ResourceHandler, title="Technology Reference", delegate=HandlerDelegate)
def technology_reference_tab(request, obj_id):
    docs = TechnologyReference.for_request(request, "handler", obj_id)
    return render(request, "technology_reference/templates/technology_reference_tab.html", docs.context())


@tab_extension(model=Environment, title="Technology Reference", delegate=EnvironmentDelegate)
def technology_reference_env_tab(request, obj_id):
    docs = TechnologyReference.for_request(request, "environment", obj_id)
    return render(request, "technology_reference/templates/technology_reference_tab.html", docs.context())


def technology_reference_export(request, scope, obj_id):
    docs = TechnologyReference.for_request(request, scope, obj_id)
    response = HttpResponse(
        docs.xlsx(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="technology_reference_{docs.type_slug or docs.handler.id}.xlsx"'
    )
    return response
