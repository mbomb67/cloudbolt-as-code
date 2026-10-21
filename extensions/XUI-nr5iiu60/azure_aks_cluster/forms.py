from django import forms
from django.core.exceptions import ValidationError

from common.forms import C2Form
from common.widgets import CheckboxSelectMultipleTable

# Scale methods as the Azure portal names them on a node pool's Scale page.
SCALE_METHOD_CHOICES = [
    ("Manual", "Manual: run a fixed number of nodes"),
    ("Autoscale", "Autoscale: let the cluster autoscaler keep the pool between a minimum and a maximum"),
]

# A node pool holds at most 1,000 nodes; a User pool can scale to 0, a System
# pool keeps at least one node.
#   https://learn.microsoft.com/en-us/azure/aks/scale-node-pools
#   https://learn.microsoft.com/en-us/azure/aks/use-system-pools
MAX_NODES = 1000


class ScaleNodePoolForm(C2Form):
    """Scale method and node counts for one node pool (prefilled from Azure)."""

    scale_method = forms.ChoiceField(label="Scale method", choices=SCALE_METHOD_CHOICES)
    node_count = forms.IntegerField(
        label="Node count",
        required=False,
        min_value=0,
        max_value=MAX_NODES,
        help_text="Used with Manual: the number of nodes the pool runs.",
    )
    min_count = forms.IntegerField(
        label="Minimum node count",
        required=False,
        min_value=0,
        max_value=MAX_NODES,
        help_text="Used with Autoscale: the autoscaler never scales in below this many nodes.",
    )
    max_count = forms.IntegerField(
        label="Maximum node count",
        required=False,
        min_value=1,
        max_value=MAX_NODES,
        help_text="Used with Autoscale: the autoscaler never scales out above this many nodes.",
    )

    def __init__(self, *args, **kwargs):
        self.system_pool = bool(kwargs.pop("system_pool", False))
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        method = cleaned.get("scale_method")
        floor = 1 if self.system_pool else 0
        if method == "Manual":
            count = cleaned.get("node_count")
            if count is None:
                raise ValidationError("Enter the node count.")
            if count < floor:
                raise ValidationError("A System node pool needs at least one node.")
        elif method == "Autoscale":
            minimum = cleaned.get("min_count")
            maximum = cleaned.get("max_count")
            if minimum is None or maximum is None:
                raise ValidationError("Enter the minimum and maximum node counts.")
            if minimum < floor:
                raise ValidationError("A System node pool needs a minimum of at least one node.")
            if minimum > maximum:
                raise ValidationError("The minimum node count cannot exceed the maximum.")
        return cleaned


class AddClusterGroupsForm(C2Form):
    """CloudBolt groups to entitle to a cluster's environment (those not entitled yet)."""

    groups = forms.MultipleChoiceField(
        label="",
        widget=CheckboxSelectMultipleTable,
        error_messages={"required": "Choose at least one group."},
    )

    def __init__(self, *args, **kwargs):
        choices = kwargs.pop("choices", [])
        super().__init__(*args, **kwargs)
        self.fields["groups"].choices = choices


class ConfirmForm(C2Form):
    """No fields: submitting the dialog confirms."""
