"""
Generate options for 'POC Application Tier' (Generated Parameter Options plugin).

Fills the poc_application_tier dropdown on the POC Server blueprint from
Python instead of a static list, which is the test plan's "Python-backed
extensibility" item (4.3.1). Swap the list for a lookup against a CMDB or
an API to show live options.
"""

TIERS = [
    ("web", "Web tier"),
    ("app", "Application tier"),
    ("db", "Database tier"),
]


def get_options_list(field, environment=None, group=None, **kwargs):
    options = list(TIERS)
    if group is not None and "prod" in (group.name or "").lower():
        # A production group does not get the database tier from the catalog.
        options = [o for o in options if o[0] != "db"]
    return {"options": options, "initial_value": options[0][0]}
