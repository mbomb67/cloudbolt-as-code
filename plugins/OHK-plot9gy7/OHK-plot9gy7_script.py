"""
Display condition for the Extend Expiration resource actions: the action is
shown only on a resource that has an expiration_date value, so it stays off
resources whose blueprint sets no Expiration Date.
"""


def should_display(resource, request=None):
    return bool(resource.get_value_for_custom_field("expiration_date"))
