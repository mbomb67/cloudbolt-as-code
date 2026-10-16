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
LDAP Utility's domain is read with a paged LDAP search, under the naming
context the directory server itself reports, and kept in CloudBolt's cache for
CACHE_SECONDS, so typing does not search the directory per keystroke
and a new OU shows up within that time.

Each choice's value is the OU's distinguished name, the form Add-Computer
-OUPath and the vCenter guest customization take. Its domain is in its DC
components, so the join action finds the domain and its LDAP Utility from the
OU alone.

CloudBolt passes suggest_options only the field and the typed text, not other
values on the form, so it cannot be narrowed by another parameter. A domain
whose search fails or finds no OUs adds a choice with an empty value that says
why, when nothing matched at all or when the typed text is that domain's name,
so a broken LDAP Utility is visible on the form instead of returning nothing.

get_options_list returns None, so CloudBolt keeps no option list of its own for
domain_ou and accepts an OU passed in through the API. The plug-in's metadata
lists domain_ou under parameters, which links this action to that parameter
when the repository is synced.

This runs in the web server, which keeps a synced shared module until it
restarts, so the search, including the naming context lookup, lives here and
uses only long-standing ad_domain helpers.

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


def _naming_context(conn, ldap_utility):
    """
    The root of the domain the directory server holds: its root DSE's
    defaultNamingContext (https://learn.microsoft.com/en-us/windows/win32/adschema/rootdse).
    The LDAP Utility's Domain field is often a sign-in or e-mail domain, not the
    AD domain, so it is not used. Falls back to the utility's Base DN.
    """
    try:
        for _, entry in conn.search_ext_s("", ldap.SCOPE_BASE, "(objectClass=*)", ["defaultNamingContext"]):
            values = (entry or {}).get("defaultNamingContext")
            if values:
                return values[0].decode("utf-8")
    except ldap.LDAPError:
        pass
    return ldap_utility.base_dn


def _domain_ous(ldap_utility):
    """(search base, every OU DN under it) for the utility, from the cache or a paged search."""
    key = "suggest_domain_ous_v3_%s" % ldap_utility.id
    found = cache.get(key)
    if found is not None:
        return found["base"], found["dns"]

    dns = []
    # Not critical, so a directory without paged results still answers.
    page = SimplePagedResultsControl(False, size=PAGE_SIZE, cookie="")
    conn = ad_domain.connect(ldap_utility)
    try:
        base = _naming_context(conn, ldap_utility)
        while True:
            msgid = conn.search_ext(
                base, ldap.SCOPE_SUBTREE, "(objectClass=organizationalUnit)", ["ou"], serverctrls=[page]
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

    if dns:
        # An empty answer is not cached, so a fixed LDAP Utility shows its OUs at once.
        try:
            cache.set(key, {"base": base, "dns": dns}, CACHE_SECONDS)
        except Exception as err:
            # A very large directory can exceed the cache's item size; search again next time.
            logger.warning("Could not cache %d OUs under %s: %s", len(dns), base, err)
    return base, dns


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
    problems = []
    for ldap_utility in utilities:
        label = (ldap_utility.ldap_domain or "").strip()
        try:
            base, dns = _domain_ous(ldap_utility)
        except Exception as err:
            logger.warning("OU search with LDAP Utility %s failed: %s", label, err)
            problems.append(([label], "OU search with LDAP Utility %s failed: %s" % (label, err)))
            continue
        if not dns:
            names = [label, ad_domain.dn_domain(base)]
            problems.append((names, "LDAP Utility %s found no OUs under %s" % (label, base)))
        for dn in dns:
            path = ad_domain.canonical_name(dn)
            if needle in dn.lower() or needle in path.lower():
                own_name = path.rsplit("/", 1)[-1].lower()
                hits[dn.lower()] = (0 if needle in own_name else 1, path.lower(), dn, path)

    options = [(dn, path) for _, _, dn, path in sorted(hits.values())[:MAX_RESULTS]]
    # An LDAP Utility whose search failed or found nothing says so when nothing
    # matched at all, or when the user is typing its domain's name.
    matched = bool(options)
    for names, problem in problems:
        typed = any(name and (needle in name.lower() or name.lower() in needle) for name in names)
        if not matched or typed:
            options.append(("", problem[:300]))
    return options
