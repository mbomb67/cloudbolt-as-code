"""
CloudBolt orchestration plug-in: join a Linux server to Active Directory.

Runs at the Post-Provision hook point. For each Linux server in the job it:
  - skips Windows servers and servers with no `domain_to_join` set;
  - reads the server's `domain_to_join` custom field, which is LDAP-typed and
    yields an LDAPUtility directly (domain, bind account, bind password);
  - reads the optional `domain_ou` custom field for computer-object placement;
  - renders an OS-native bash join script (realmd + SSSD + adcli) and runs it on
    the server via `server.execute_script(run_with_sudo=True)`;
  - maps the script's exit code (read from CommandExecutionException.rv) to a
    diagnostic message, scrubbing the join password from anything returned.

Security: the join password is fed to `realm join` via stdin inside the script
(never on argv, never echoed) and scrubbed from the returned output_message,
because execute_script offers no out-of-band secret channel and logs
stdout/stderr at INFO. The password is base64-encoded in the script body only
for shell-quoting safety, not secrecy.

NOTE: this file must contain no Jinja delimiters (double curly-brace
expressions, brace-percent statements, or brace-hash comments) anywhere,
including the embedded bash, because CloudBolt renders the plug-in source as a
template before executing it. The bash uses only single-brace shell refs.
"""

import base64
import shlex

from common.methods import set_progress
from infrastructure.models import CustomField
from utilities.exceptions import CommandExecutionException
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Remote-execution timeout (seconds). Package install + realm join over a slow
# mirror can take a while; generous but bounded.
EXECUTE_TIMEOUT = 1800

# Exit codes emitted by the join script, mapped to operator-facing diagnostics.
EXIT_CODE_DIAGNOSTICS = {
    2: "Unsupported OS family (not RHEL- or Debian-family).",
    3: "DNS/realm discovery failed - verify the VM resolver points at AD DNS "
       "and the domain's SRV records resolve.",
    4: "Post-join verification failed - realm join returned 0 but the host is "
       "not enrolled.",
}

# Static bash body. Plain string (NOT an f-string) so '${VAR}' shell refs are
# left intact and no Jinja tokens are introduced. The injected header (built in
# _build_join_script) defines AD_DOMAIN / AD_JOIN_USER / AD_OU / AD_JOIN_PASSWORD
# before this body runs.
_JOIN_BODY = r"""
log(){ echo "[ad-join] $*"; }

# --- 0. Idempotency: stop if already joined to this domain ---
if command -v realm >/dev/null 2>&1 && \
   realm list 2>/dev/null | grep -qi "domain-name: *${AD_DOMAIN}"; then
  log "Already joined to ${AD_DOMAIN}; nothing to do."
  exit 0
fi

# --- 1. Detect OS family ---
. /etc/os-release
FAMILY="unknown"
case " ${ID:-} ${ID_LIKE:-} " in
  *" rhel "*|*" fedora "*|*" centos "*|*ol*|*rocky*|*almalinux*) FAMILY="rhel" ;;
  *" debian "*|*" ubuntu "*)                                     FAMILY="debian" ;;
esac
log "OS family: ${FAMILY} (ID=${ID:-?} VERSION_ID=${VERSION_ID:-?})"

# --- 2. Install native packages from base repos (with a short retry loop) ---
install_with_retry(){
  local n=1
  while [ "${n}" -le 3 ]; do
    if "$@"; then return 0; fi
    log "Package install attempt ${n} failed; retrying..."
    n=$((n+1))
    sleep 5
  done
  return 1
}

if [ "${FAMILY}" = "rhel" ]; then
  PKGS="realmd sssd adcli oddjob oddjob-mkhomedir samba-common-tools krb5-workstation"
  command -v authselect >/dev/null 2>&1 && PKGS="${PKGS} authselect-compat"
  install_with_retry dnf install -y ${PKGS} || { log "ERROR: package install failed."; exit 5; }
elif [ "${FAMILY}" = "debian" ]; then
  export DEBIAN_FRONTEND=noninteractive
  install_with_retry apt-get update -y || { log "ERROR: apt-get update failed."; exit 5; }
  install_with_retry apt-get install -y realmd sssd sssd-tools adcli krb5-user \
                     samba-common-bin libpam-sss libnss-sss packagekit \
    || { log "ERROR: package install failed."; exit 5; }
else
  log "ERROR: unsupported OS family."; exit 2
fi

# --- 3. Preflight: time sync + DNS discovery ---
command -v chronyc >/dev/null 2>&1 && chronyc makestep >/dev/null 2>&1 || true
if ! realm discover "${AD_DOMAIN}" >/dev/null 2>&1; then
  log "ERROR: realm discover failed for ${AD_DOMAIN}."
  log "       Verify the VM resolver points at AD DNS and SRV records resolve."
  exit 3
fi

# --- 4. Join (password from stdin -> never appears in argv, ps, or logs) ---
# Build args as an array so an OU distinguished name containing spaces
# (e.g. "OU=Domain Controllers") is passed as a single argument, not word-split.
JOIN_ARGS=(--user="${AD_JOIN_USER}")
[ -n "${AD_OU}" ] && JOIN_ARGS+=(--computer-ou="${AD_OU}")
JOIN_ARGS+=("${AD_DOMAIN}")
log "Joining ${AD_DOMAIN} ..."
printf '%s' "${AD_JOIN_PASSWORD}" | realm join "${JOIN_ARGS[@]}"
log "Join command returned 0."

# --- 5. First-login home dirs + short-name logins ---
if [ "${FAMILY}" = "rhel" ]; then
  authselect enable-feature with-mkhomedir >/dev/null 2>&1 || true
  systemctl enable --now oddjobd >/dev/null 2>&1 || true
else
  pam-auth-update --enable mkhomedir >/dev/null 2>&1 || \
    grep -q pam_mkhomedir /etc/pam.d/common-session || \
    echo "session optional pam_mkhomedir.so skel=/etc/skel umask=077" \
      >> /etc/pam.d/common-session
fi

SSSD_CONF="/etc/sssd/sssd.conf"
if grep -q '^use_fully_qualified_names' "${SSSD_CONF}" 2>/dev/null; then
  sed -i 's/^use_fully_qualified_names.*/use_fully_qualified_names = False/' "${SSSD_CONF}"
else
  sed -i "/^\[domain\/${AD_DOMAIN}\]/a use_fully_qualified_names = False" "${SSSD_CONF}"
fi
systemctl restart sssd

# --- 6. Verify ---
if realm list | grep -qi "domain-name: *${AD_DOMAIN}"; then
  log "SUCCESS: joined ${AD_DOMAIN}."
  exit 0
fi
log "ERROR: post-join verification failed."
exit 4
"""


def _ensure_custom_fields():
    """Idempotently create the optional `domain_ou` parameter.

    `domain_to_join` is already seeded by CloudBolt as an LDAP-typed field, so
    it is intentionally NOT (re)defined here.
    """
    CustomField.objects.get_or_create(
        name="domain_ou",
        defaults=dict(
            label="Domain OU",
            description="Distinguished name of the AD OU to place this server's "
                        "computer object in (e.g. OU=Linux,OU=Servers,DC=corp,DC=example,DC=com). "
                        "Optional; defaults to the domain's Computers container.",
            type="STR",
            show_on_servers=True,
        ),
    )


def _build_join_script(domain, join_user, ou, password):
    """Render the bash join script with safely-quoted, injected inputs.

    The password is base64-encoded for shell-quoting safety (it may contain any
    character) and decoded on the VM into a shell variable that is piped to
    `realm join` via stdin.
    """
    b64 = base64.b64encode(password.encode("utf-8")).decode("ascii")
    header = "\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "AD_DOMAIN=" + shlex.quote(domain),
        "AD_JOIN_USER=" + shlex.quote(join_user),
        "AD_OU=" + shlex.quote(ou or ""),
        'AD_JOIN_PASSWORD="$(printf %s ' + shlex.quote(b64) + ' | base64 -d)"',
    ])
    return header + "\n" + _JOIN_BODY


def _scrub(text, secret):
    """Remove the cleartext secret from any text before it is returned/logged."""
    if not text or not secret:
        return text or ""
    return str(text).replace(secret, "***")


def _join_one_server(server):
    """Join a single server. Returns (status, message) where status is one of
    SUCCESS / FAILURE / SKIPPED."""
    hostname = getattr(server, "hostname", None) or str(server)

    if server.is_windows():
        return "SKIPPED", "{}: Windows server, skipping AD domain join.".format(hostname)

    ldap_util = server.get_value_for_custom_field("domain_to_join")
    if ldap_util is None:
        return "SKIPPED", "{}: no domain_to_join set, skipping AD domain join.".format(hostname)

    domain = ldap_util.ldap_domain
    join_user = ldap_util.serviceaccount
    password = ldap_util.servicepasswd
    ou = server.get_value_for_custom_field("domain_ou")

    if not domain or not join_user or not password:
        return "FAILURE", ("{}: LDAPUtility for domain_to_join is missing domain, "
                           "account, or password.".format(hostname))

    set_progress("{}: joining Active Directory domain '{}'{}.".format(
        hostname, domain, " (OU={})".format(ou) if ou else ""))

    script = _build_join_script(domain, join_user, ou, password)

    try:
        output = server.execute_script(
            script_contents=script,
            run_with_sudo=True,
            timeout=EXECUTE_TIMEOUT,
        )
    except CommandExecutionException as exc:
        rv = getattr(exc, "rv", "")
        diag = EXIT_CODE_DIAGNOSTICS.get(rv)
        if diag is None:
            if rv == 5:
                diag = "Package installation from base repositories failed."
            else:
                diag = ("realm join failed (enrollment error - check join-account "
                        "rights on the target OU, credentials, and DC connectivity).")
        scrubbed = _scrub(getattr(exc, "output", ""), password)
        msg = "{}: AD domain join failed (exit {}): {} {}".format(
            hostname, rv, diag, scrubbed).strip()
        logger.error(_scrub(msg, password))
        return "FAILURE", msg

    safe_output = _scrub(output, password)
    logger.info("AD domain join output for %s: %s", hostname, safe_output)
    return "SUCCESS", "{}: joined Active Directory domain '{}'.".format(hostname, domain)


def run(job, *args, **kwargs):
    """Post-Provision entry point. Joins each Linux server in the job to AD."""
    _ensure_custom_fields()

    servers = list(job.server_set.all())
    if not servers:
        return "SUCCESS", "No servers in job; nothing to join.", ""

    results = [_join_one_server(server) for server in servers]

    failures = [msg for status, msg in results if status == "FAILURE"]
    joined = [msg for status, msg in results if status == "SUCCESS"]
    skipped = [msg for status, msg in results if status == "SKIPPED"]

    for status, msg in results:
        set_progress(msg)

    if failures:
        return "FAILURE", "\n".join(joined + skipped + failures), "\n".join(failures)

    summary = joined or skipped or ["No Linux servers required an AD domain join."]
    return "SUCCESS", "\n".join(summary), ""
