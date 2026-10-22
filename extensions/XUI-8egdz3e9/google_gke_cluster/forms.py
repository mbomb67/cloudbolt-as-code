from django import forms
from django.core.exceptions import ValidationError

from common.forms import C2Form
from common.widgets import CheckboxSelectMultipleTable

# Scale methods as the Google Cloud console offers them on a node pool's
# details page: a fixed size, or the cluster autoscaler.
SCALE_METHOD_CHOICES = [
    ("Manual", "Manual: run a fixed number of nodes in each zone"),
    ("Autoscale", "Autoscale: let the cluster autoscaler keep each zone between a minimum and a maximum"),
]

# GKE sizes a node pool per zone: setSize takes nodes per zone, and the
# autoscaler's minNodeCount and maxNodeCount count nodes per zone. Project
# quota caps the total.
#   https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/setSize
#   https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#NodePoolAutoscaling
MAX_NODES = 1000


class ScaleNodePoolForm(C2Form):
    """Scale method and node counts per zone for one node pool (prefilled from Google Cloud)."""

    scale_method = forms.ChoiceField(label="Scale method", choices=SCALE_METHOD_CHOICES)
    node_count = forms.IntegerField(
        label="Nodes per zone",
        required=False,
        min_value=0,
        max_value=MAX_NODES,
        help_text="Used with Manual: the number of nodes the pool runs in each of its zones.",
    )
    min_count = forms.IntegerField(
        label="Minimum nodes per zone",
        required=False,
        min_value=0,
        max_value=MAX_NODES,
        help_text="Used with Autoscale: the autoscaler never scales a zone in below this many nodes.",
    )
    max_count = forms.IntegerField(
        label="Maximum nodes per zone",
        required=False,
        min_value=1,
        max_value=MAX_NODES,
        help_text="Used with Autoscale: the autoscaler never scales a zone out above this many nodes.",
    )

    def __init__(self, *args, **kwargs):
        # The cluster's only node pool keeps at least one node per zone, so
        # the cluster's own system pods always have somewhere to run.
        self.only_pool = bool(kwargs.pop("only_pool", False))
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        method = cleaned.get("scale_method")
        floor = 1 if self.only_pool else 0
        if method == "Manual":
            count = cleaned.get("node_count")
            if count is None:
                raise ValidationError("Enter the number of nodes per zone.")
            if count < floor:
                raise ValidationError("The cluster's only node pool needs at least one node per zone.")
        elif method == "Autoscale":
            minimum = cleaned.get("min_count")
            maximum = cleaned.get("max_count")
            if minimum is None or maximum is None:
                raise ValidationError("Enter the minimum and maximum nodes per zone.")
            if minimum < floor:
                raise ValidationError("The cluster's only node pool needs a minimum of at least one node per zone.")
            if minimum > maximum:
                raise ValidationError("The minimum cannot exceed the maximum.")
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
