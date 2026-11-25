"""
Generate Options for Domain Service Account

Generated Parameter Options action for the domain_service_account parameter.
It lists every connection info labeled domain_service_account (Admin >
Connection Info, the Labels field), each shown as its name and username, with
the connection info's ID as the value. For an optional parameter CloudBolt
adds an empty choice, which keeps the server's local credentials.

The Join Windows Server to AD Domain action reads the ID after it joins a
server to its domain_ou, and makes the connection info's username and password
the server's CloudBolt credentials. That action, not this one, checks that the
username includes its domain and that a password is set, so a connection info
missing either still shows here and fails the server before its join.

The plug-in's metadata lists domain_service_account under parameters, which
links this action to that parameter when the repository is synced.

Signature (CloudBolt's contract for this hook point):
  get_options_list(field, **kwargs) -> list | dict | None
"""
from utilities.models import ConnectionInfo

# The parameter and the connection info label share this name.
LABEL = "domain_service_account"


def get_options_list(field, **kwargs):
    accounts = ConnectionInfo.objects.filter(labels__name=LABEL).order_by("name")
    return [(str(account.id), "%s (%s)" % (account.name, account.username)) for account in accounts]
