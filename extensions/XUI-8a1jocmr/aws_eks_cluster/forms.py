from django import forms
from django.core.exceptions import ValidationError

from accounts.models import Group
from common.forms import C2Form
from common.widgets import CheckboxSelectMultipleTable

# How EKS treats add-on fields that were changed on the cluster. Documented on
# https://docs.aws.amazon.com/eks/latest/APIReference/API_UpdateAddon.html
RESOLVE_CONFLICTS_CHOICES = [
    ("PRESERVE", "Preserve: keep values changed on the cluster"),
    ("OVERWRITE", "Overwrite: reset changed values to the EKS defaults"),
    ("NONE", "None: fail the update if a value was changed"),
]


class ScaleNodegroupForm(C2Form):
    """Scaling configuration for one managed node group (prefilled from AWS)."""

    min_nodes = forms.IntegerField(
        label="Minimum nodes",
        min_value=0,
        max_value=1000,
        help_text="The group never scales in below this many nodes.",
    )
    desired_nodes = forms.IntegerField(
        label="Desired nodes",
        min_value=0,
        max_value=1000,
        help_text="The number of nodes the group runs now.",
    )
    max_nodes = forms.IntegerField(
        label="Maximum nodes",
        min_value=1,
        max_value=1000,
        help_text="The group never scales out above this many nodes.",
    )

    def clean(self):
        cleaned = super().clean()
        minimum = cleaned.get("min_nodes")
        desired = cleaned.get("desired_nodes")
        maximum = cleaned.get("max_nodes")
        if None in (minimum, desired, maximum):
            return cleaned
        if minimum > maximum:
            raise ValidationError("Minimum nodes cannot exceed maximum nodes.")
        if not minimum <= desired <= maximum:
            raise ValidationError(
                "Desired nodes must be between minimum and maximum nodes."
            )
        return cleaned


class UpgradeAddonForm(C2Form):
    """Target version and conflict handling for one add-on update."""

    addon_version = forms.ChoiceField(label="Version", choices=[])
    resolve_conflicts = forms.ChoiceField(
        label="Changed fields",
        choices=RESOLVE_CONFLICTS_CHOICES,
        initial="PRESERVE",
        help_text="What EKS does with add-on settings that were changed on the cluster.",
    )

    def __init__(self, *args, **kwargs):
        versions = kwargs.pop("versions", None) or []
        super().__init__(*args, **kwargs)
        self.fields["addon_version"].choices = versions


class AddGroupsForm(C2Form):
    """Groups to entitle to the cluster's environment: every group except the
    Unassigned and tenant-default placeholders and the ones already on it,
    rendered like CloudBolt's own Edit groups dialog for an environment."""

    # Not required: the datatable dialog renders fields without their errors,
    # so the view reports an empty selection itself.
    groups = forms.ModelMultipleChoiceField(
        label="",
        queryset=Group.objects.none(),
        required=False,
        widget=CheckboxSelectMultipleTable,
    )

    def __init__(self, *args, **kwargs):
        environment = kwargs.pop("environment")
        super().__init__(*args, **kwargs)
        self.fields["groups"].queryset = Group.without_unassigned.exclude(
            id__in=environment.group_set.values_list("id", flat=True)
        ).order_by("name")
