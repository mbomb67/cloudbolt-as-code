"""Blueprint build item: issues a TLS certificate in Azure Key Vault and installs it on the web tier.

1. Creates a certificate in the chosen vault for the web tier's Route hostname and Service names.
2. Fetches it with its private key, which Key Vault returns only through the certificate's secret.
3. Pushes both to the VM through the OpenShift API tunnel, points mod_ssl at them and restarts httpd.
4. Gives the Route the certificate, so the router trusts the VM it re-encrypts to.

The private key goes from the vault to the VM inside the tunnel and is never logged or stored in
CloudBolt. The order form asks for an Azure environment the ordering group may use, then for a
vault in that environment's subscription and region; the environment's resource handler is the
identity that talks to the vault. Key Vault calls are in shared_modules/SHM-ny8h0nx0.

Inputs: tier (default Web), azure_env_id, key_vault (the vault's URI).
"""

from accounts.models import Group
from common.methods import set_progress
from infrastructure.models import Environment
from shared_modules import azure_key_vault_certificates as kv
from shared_modules import openshift_vm_network as vmnet
from shared_modules import vm_tier_scripts as tiers

TIER = tiers.clean("{{ tier }}", "Web")
AZURE_ENV_ID = tiers.clean("{{ azure_env_id }}")
KEY_VAULT = tiers.clean("{{ key_vault }}")

CERT_PATH = "/etc/pki/tls/certs/web-app.crt"
KEY_PATH = "/etc/pki/tls/private/web-app.key"


def _azure_environments(group):
    """Azure environments the group may order into. See docs/agents/rbac-and-security.md."""
    if group is not None and not isinstance(group, Group):
        group = Group.objects.filter(name=str(group)).first()
    if not group:
        return Environment.objects.none()
    available = [environment.id for environment in group.get_available_environments()]
    return Environment.objects.filter(
        id__in=available, resource_handler__azurearmhandler__isnull=False
    ).order_by("name")


def _azure_handler(env_id):
    """(environment, cast Azure handler) for an environment id; raises ValueError when it is not Azure."""
    from resourcehandlers.azure_arm.models import AzureARMHandler

    environment = Environment.objects.filter(id=int(env_id)).first() if str(env_id).isdigit() else None
    handler = environment.resource_handler.cast() if environment and environment.resource_handler else None
    if not isinstance(handler, AzureARMHandler):
        raise ValueError(f"Environment '{env_id}' is not an Azure environment.")
    return environment, handler


def generate_options_for_azure_env_id(field=None, group=None, **kwargs):
    environments = _azure_environments(group)
    if not environments.exists():
        return [("", "------ No Azure environments available to this group ------")]
    return [(environment.id, environment.name) for environment in environments]


def generate_options_for_key_vault(field=None, control_value=None, **kwargs):
    """Vaults in the chosen environment's subscription and region (control_value is azure_env_id)."""
    if not control_value:
        return [("", "------ Choose an Azure environment first ------")]
    try:
        environment, handler = _azure_handler(control_value)
        vaults = kv.list_vaults(handler, location=environment.node_location or None)
    except (ValueError, kv.KeyVaultError) as error:
        return [("", f"------ Could not list key vaults: {str(error)[:120]} ------")]
    if not vaults:
        return [("", f"------ No key vaults in {environment.node_location or 'this subscription'} ------")]
    return [(vault["uri"], f"{vault['name']} ({vault['location']})") for vault in vaults]


def run(job, resource=None, **kwargs):
    if not AZURE_ENV_ID or not KEY_VAULT.startswith("https://"):
        return "FAILURE", "", "Choose an Azure environment and a key vault on the order form."
    try:
        _, azure = _azure_handler(AZURE_ENV_ID)
        server = tiers.tier_server(resource, TIER)
        cluster, namespace = vmnet.for_server(server)
        route = vmnet.get_route(cluster, namespace, server.hostname)
    except (ValueError, tiers.TierError, vmnet.OpenShiftApiError) as error:
        return "FAILURE", "", str(error)

    # The Route hostname is what browsers ask for; the Service names are what the router checks
    # when it re-encrypts to the VM.
    route_host = (route or {}).get("spec", {}).get("host")
    dns_names = [name for name in (
        route_host,
        f"{server.hostname}.{namespace}.svc",
        vmnet.service_fqdn(server.hostname, namespace),
    ) if name]
    # Unique per deployment, so a hostname CloudBolt reuses never meets a soft-deleted certificate.
    name = kv.certificate_name(f"{server.hostname}-{vmnet.owner_labels(resource)[vmnet.OWNER_LABEL]}")

    set_progress(f"Creating certificate {name} in {KEY_VAULT} for {', '.join(dns_names)}")
    try:
        certificate = kv.issue_certificate(azure, KEY_VAULT, name, dns_names)
    except kv.KeyVaultError as error:
        return "FAILURE", "", str(error)
    set_progress(f"Key Vault issued {certificate['id']}")
    tiers.remember(resource, "web_app_certificate", certificate["id"], "Key Vault Certificate", show=True)
    tiers.remember(resource, "web_app_key_vault", KEY_VAULT, "Key Vault")
    tiers.remember(resource, "web_app_azure_env_id", int(AZURE_ENV_ID), "Azure Environment ID", "INT")

    script = (
        tiers.write_file(CERT_PATH, certificate["certificate_pem"])
        + tiers.write_file(KEY_PATH, certificate["private_key_pem"], mode="0600")
        + f"sudo sed -i -E 's|^SSLCertificateFile .*|SSLCertificateFile {CERT_PATH}|; "
        f"s|^SSLCertificateKeyFile .*|SSLCertificateKeyFile {KEY_PATH}|' /etc/httpd/conf.d/ssl.conf\n"
        + "sudo apachectl configtest\n"
        + "sudo systemctl restart httpd\n"
        + "sleep 2\n"
        + "echo '== Certificate httpd now serves'\n"
        + "echo | openssl s_client -connect localhost:443 2>/dev/null"
        " | openssl x509 -noout -subject -issuer -enddate -fingerprint -sha1 -ext subjectAltName\n"
    )
    set_progress(f"Installing the certificate on {server.hostname}")
    try:
        tiers.run(server, script)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)

    if route:
        try:
            vmnet.set_route_destination_ca(cluster, namespace, server.hostname, certificate["certificate_pem"])
        except vmnet.OpenShiftApiError as error:
            return "FAILURE", "", str(error)
        set_progress(f"Route {server.hostname} now trusts the Key Vault certificate on the VM")

    return "SUCCESS", f"{server.hostname} serves HTTPS with Key Vault certificate {certificate['id']}", ""
