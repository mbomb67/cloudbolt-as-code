from django import forms
from django.core.exceptions import ValidationError

from common.forms import C2Form

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
