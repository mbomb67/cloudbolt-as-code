"""
Generate options for 'Application' (Generated Parameter Options plugin).

Fills the application dropdown on the Standard Server blueprint from Python
instead of a static list, which is the test plan's "Python-backed
extensibility" item (4.3.1). Every group sees the same list; replace it with
a lookup against a CMDB (for example the business applications in
ServiceNow) to show live options.
"""

APPLICATIONS = [
    "Payroll",
    "Customer Portal",
    "Data Warehouse",
    "Database",
    "CRM",
    "E-commerce Storefront",
    "Internal Wiki",
]


def get_options_list(field, environment=None, group=None, **kwargs):
    options = [(name, name) for name in APPLICATIONS]
    return {"options": options, "initial_value": options[0][0]}
