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
the text is matched, ignoring case, anywhere in each OU's distinguished name or
path (corp.example.com/Servers/Web), so a parent OU or the domain name finds
the OUs under it. OUs whose own name matches come first. The OU list of each
LDAP Utility's domain is read with a paged LDAP search and kept in CloudBolt's
cache for CACHE_SECONDS, so typing does not search the directory per keystroke
and a new OU shows up within that time.

Each choice's value is the OU's distinguished name, the form Add-Computer
-OUPath and the vCenter guest customization take. Its domain is in its DC
components, so the join action finds the domain and its LDAP Utility from the
OU alone.

CloudBolt passes suggest_options only the field and the typed text, not other
values on the form, so it cannot be narrowed by another parameter. When every
directory search fails, the one choice returned says why, instead of an empty
list.

get_options_list returns None, so CloudBolt keeps no option list of its own for
domain_ou and accepts an OU passed in through the API. The plug-in's metadata
lists domain_ou under parameters, which links this action to that parameter
when the repository is synced.

This runs in the web server, which keeps a synced shared module until it
restarts, so the search lives here and uses only long-standing ad_domain
helpers.

References:
  Paged results control: https://www.python-ldap.org/en/latest/reference/ldap-controls.html
  LDAPObject.result3:    https://www.python-ldap.org/en/latest/reference/ldap.html
"""
import ldap
from django.core.cache import cache
from ldap.controls import SimplePagedResultsControl

from utilities.logger import ThreadLogger

from shared_modules import ad_domain

logger = ThreadLogger(__name__)

MIN_QUERY_LENGTH = 3
MAX_RESULTS = 25
CACHE_SECONDS = 300
PAGE_SIZE = 500
MAX_OUS = 20000


def get_options_list(field, **kwargs):
    """No fixed options: the parameter takes whatever OU the user picked or typed."""
    return None


def _domain_ous(ldap_utility):
    """Every OU DN in the utility's domain, from the cache or a paged search."""
    key = "suggest_domain_ous_%s" % ldap_utility.id
    dns = cache.get(key)
    if dns is not None:
        return dns

    dns = []
    page = SimplePagedResultsControl(True, size=PAGE_SIZE, cookie="")
    conn = ad_domain.connect(ldap_utility)
    try:
        while True:
            msgid = conn.search_ext(
                ad_domain.search_base(ldap_utility),
                ldap.SCOPE_SUBTREE,
                "(objectCategory=organizationalUnit)",
                ["ou"],
                serverctrls=[page],
            )
            _, data, _, controls = conn.result3(msgid, 1, ad_domain.LDAP_TIMEOUT)
            dns.extend(dn for dn, _ in data if dn)
            cookie = next(
                (c.cookie for c in controls if c.controlType == SimplePagedResultsControl.controlType),
                None,
            )
            if not cookie or len(dns) >= MAX_OUS:
                break
            page.cookie = cookie
    finally:
        conn.unbind_s()

    try:
        cache.set(key, dns, CACHE_SECONDS)
    except Exception as err:
        # A very large directory can exceed the cache's item size; search again next time.
        logger.warning("Could not cache %d OUs of %s: %s", len(dns), ldap_utility.ldap_domain, err)
    return dns


def suggest_options(field, query=None, **kwargs):
    """(distinguished name, path) pairs for the OUs whose DN or path contains query."""
    text = str(query or "").strip()
    if ad_domain.is_dn(text):
        # A value that is already a DN: CloudBolt is asking for its label.
        return [(text, ad_domain.canonical_name(text))]
    if len(text) < MIN_QUERY_LENGTH:
        return []

    utilities = ad_domain.utilities()
    if not utilities:
        return [("", "No LDAP Utility is configured (Admin > LDAP Authentication)")]

    needle = text.lower()
    hits = {}
    errors = []
    for ldap_utility in utilities:
        try:
            dns = _domain_ous(ldap_utility)
        except Exception as err:
            logger.warning("OU search in %s failed: %s", ldap_utility.ldap_domain, err)
            errors.append("%s: %s" % (ldap_utility.ldap_domain, err))
            continue
        for dn in dns:
            path = ad_domain.canonical_name(dn)
            if needle in dn.lower() or needle in path.lower():
                own_name = path.rsplit("/", 1)[-1].lower()
                hits[dn.lower()] = (0 if needle in own_name else 1, path.lower(), dn, path)

    if not hits and errors and len(errors) == len(utilities):
        return [("", "OU search failed. " + "; ".join(errors)[:300])]
    ranked = sorted(hits.values())[:MAX_RESULTS]
    return [(dn, path) for _, _, dn, path in ranked]
