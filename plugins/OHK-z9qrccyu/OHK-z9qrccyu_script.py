"""
Suggest Domain OUs

Generated Parameter Options action for the domain_ou parameter. When the action
for a parameter defines suggest_options, CloudBolt draws the parameter as a
type-ahead field and calls suggest_options(field, query) as the user types:
the order form through /suggest_options_for_selectize/ (orders/views.py), a
custom form through its suggestOptions endpoint (customforms/api/v3/services.py),
and once with the saved value when a form opens on an existing one
(common/fields.py).

Nothing is returned until the user has typed MIN_QUERY_LENGTH characters. Then
every LDAP Utility's domain is searched for organizational units whose name
contains the text. Each choice's value is the OU's distinguished name, the form
Add-Computer -OUPath and the vCenter guest customization take, and its label is
the OU's path, starting with its domain.

CloudBolt passes suggest_options only the field and the typed text, not the
other values on the form, so the search cannot be narrowed to the domain the
user picked in domain_to_join. With one LDAP Utility that makes no difference.
With several, every domain is searched, and the join action rejects an OU that
is not in the server's domain_to_join.

get_options_list returns None, so CloudBolt keeps no option list of its own for
domain_ou and accepts an OU passed in through the API.

The plug-in's metadata lists domain_ou under parameters, which links this
action to that parameter when the repository is synced.
"""
from utilities.logger import ThreadLogger

from shared_modules import ad_domain

logger = ThreadLogger(__name__)

MIN_QUERY_LENGTH = 3
MAX_RESULTS = 25


def get_options_list(field, **kwargs):
    """No fixed options: the parameter takes whatever OU the user picked or typed."""
    return None


def suggest_options(field, query=None, **kwargs):
    """(distinguished name, path) pairs for the OUs whose name contains query."""
    text = str(query or "").strip()
    if ad_domain.is_dn(text):
        # A value that is already a DN: CloudBolt is asking for its label.
        return [(text, ad_domain.canonical_name(text))]
    if len(text) < MIN_QUERY_LENGTH:
        return []

    found = {}
    for ldap_utility in ad_domain.utilities():
        try:
            for dn in ad_domain.search_ous(ldap_utility, text, MAX_RESULTS):
                found[dn.lower()] = (dn, ad_domain.canonical_name(dn))
        except Exception as err:
            logger.warning(
                "OU search in %s for '%s' failed: %s", ldap_utility.ldap_domain, text, err
            )

    options = sorted(found.values(), key=lambda option: option[1].lower())
    return options[:MAX_RESULTS]
