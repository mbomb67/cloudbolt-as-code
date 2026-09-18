#!/usr/bin/env bash
#
# CloudBolt catalog item post-provision script
# "PostgreSQL Database Service" - Oracle Linux 8 (x86_64 / aarch64)
#
# Installs PostgreSQL from the PGDG repository, initializes the cluster,
# applies listen/port/auth configuration, and creates one application
# database plus an owning role.
#
# Idempotent: safe to re-run on the same server.
#
# Notes for CloudBolt integration:
#   - Every variable below reads from the environment first, so you can either
#     replace the defaults with parameter substitution or export the
#     values from a CloudBolt plugin before invoking the script.
#   - DB_OWNER_PASSWORD should come from an encrypted/password-type parameter.
#     It is never echoed to stdout or the log file.
#   - Exit code 0 = success. Any failure aborts with a non-zero code and the
#     failing line number so CloudBolt marks the job FAILURE.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# VARIABLES  (override via CloudBolt parameters / environment)
# ---------------------------------------------------------------------------
PG_MAJOR="{{resource.postgres_version}}"                       # PGDG majors on EL8: 14,15,16,17,18
DB_NAME="{{resource.postgres_database_name}}"
DB_OWNER="{{resource.postgres_database_owner}}"
DB_OWNER_PASSWORD="{{resource.postgres_database_password}}"

PG_PORT="{{resource.postgres_port}}"                     # 5432
LISTEN_ADDRESSES="*"        # '*' = all interfaces, or 'localhost'
CLIENT_CIDR="{{resource.postgres_client_cidr}}"         # network allowed to authenticate - i.e. 10.0.0.0/8
PGDATA_DIR="/var/lib/pgsql/${PG_MAJOR}/data"

MAX_CONNECTIONS="200"
SHARED_BUFFERS="256MB"
TIMEZONE="UTC"

INSTALL_CONTRIB="true"       # true = also install *-contrib
OPEN_FIREWALL="true"           # true = add firewalld rule for PG_PORT
LOG_FILE="/var/log/cb_postgres_install.log"

# ---------------------------------------------------------------------------
# Derived values / helpers - no need to edit below for normal use
# ---------------------------------------------------------------------------
ARCH="$(uname -m)"
PGDG_REPO_RPM="https://download.postgresql.org/pub/repos/yum/reporpms/EL-8-${ARCH}/pgdg-redhat-repo-latest.noarch.rpm"
PG_BIN="/usr/pgsql-${PG_MAJOR}/bin"
PSQL="/usr/pgsql-${PG_MAJOR}/bin/psql"
PG_SERVICE="postgresql-${PG_MAJOR}"
DEFAULT_PGDATA="/var/lib/pgsql/${PG_MAJOR}/data"

log()  { printf '%s [INFO ] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE"; }
warn() { printf '%s [WARN ] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE" >&2; }
die()  { printf '%s [ERROR] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE" >&2; exit 1; }
trap 'die "Failed at line $LINENO"' ERR

as_postgres() { ( cd /tmp && runuser -u postgres -- "$@" ); }

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
touch "$LOG_FILE" 2>/dev/null || LOG_FILE=/dev/null
log "=== PostgreSQL ${PG_MAJOR} install starting on $(hostname -f 2>/dev/null || hostname) ==="

[[ "$(id -u)" -eq 0 ]] || die "This script must run as root (CloudBolt: run as root or via sudo)."

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  log "Detected OS: ${PRETTY_NAME:-unknown}"
  case "${VERSION_ID:-}" in
    8*) : ;;
    *)  warn "Expected an EL8 release (Oracle Linux 8); found VERSION_ID='${VERSION_ID:-none}'. Continuing." ;;
  esac
else
  warn "/etc/os-release not readable - skipping OS validation."
fi

# ---------------------------------------------------------------------------
# 2. Repositories (EPEL is a dependency of some PGDG packages)
# ---------------------------------------------------------------------------
if ! dnf repolist enabled 2>/dev/null | grep -qiE 'epel'; then
  log "Enabling EPEL"
  dnf install -y oracle-epel-release-el8 \
    || dnf install -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm \
    || warn "Could not enable EPEL - continuing (only needed for some optional packages)."
else
  log "EPEL already enabled"
fi

if ! rpm -q pgdg-redhat-repo >/dev/null 2>&1; then
  log "Installing PGDG repository RPM: ${PGDG_REPO_RPM}"
  dnf install -y "$PGDG_REPO_RPM"
else
  log "PGDG repository already installed"
fi

# Mandatory on EL8: the AppStream 'postgresql' module hides PGDG server packages.
log "Disabling the built-in postgresql DNF module"
dnf -qy module disable postgresql

# ---------------------------------------------------------------------------
# 3. Packages
# ---------------------------------------------------------------------------
PKGS=("postgresql${PG_MAJOR}-server" "postgresql${PG_MAJOR}")
[[ "${INSTALL_CONTRIB,,}" == "true" ]] && PKGS+=("postgresql${PG_MAJOR}-contrib")
log "Installing packages: ${PKGS[*]}"
dnf install -y "${PKGS[@]}"

# Needed for SELinux relabeling / non-default port
if [[ "$PGDATA_DIR" != "$DEFAULT_PGDATA" || "$PG_PORT" != "5432" ]]; then
  rpm -q policycoreutils-python-utils >/dev/null 2>&1 || dnf install -y policycoreutils-python-utils
fi

# ---------------------------------------------------------------------------
# 4. Custom data directory (optional) - systemd override + SELinux label
# ---------------------------------------------------------------------------
if [[ "$PGDATA_DIR" != "$DEFAULT_PGDATA" ]]; then
  log "Configuring custom data directory: ${PGDATA_DIR}"
  install -d -o postgres -g postgres -m 0700 "$PGDATA_DIR"
  install -d -m 0755 "/etc/systemd/system/${PG_SERVICE}.service.d"
  cat > "/etc/systemd/system/${PG_SERVICE}.service.d/override.conf" <<EOF
[Service]
Environment=PGDATA=${PGDATA_DIR}
EOF
  systemctl daemon-reload
  if command -v semanage >/dev/null 2>&1 && [[ "$(getenforce 2>/dev/null || echo Disabled)" != "Disabled" ]]; then
    semanage fcontext -a -e /var/lib/pgsql "$PGDATA_DIR" 2>/dev/null || true
    restorecon -R "$PGDATA_DIR"
  fi
fi

# ---------------------------------------------------------------------------
# 5. Initialize the cluster (skip if already initialized)
# ---------------------------------------------------------------------------
if [[ -f "${PGDATA_DIR}/PG_VERSION" ]]; then
  log "Cluster already initialized at ${PGDATA_DIR} - skipping initdb"
else
  log "Running initdb (checksums + UTF-8)"
  PGSETUP_INITDB_OPTIONS="--data-checksums --encoding=UTF8 --locale=en_US.UTF-8" \
    "${PG_BIN}/postgresql-${PG_MAJOR}-setup" initdb
fi

# ---------------------------------------------------------------------------
# 6. postgresql.conf - managed settings in a conf.d include file
# ---------------------------------------------------------------------------
CONF_MAIN="${PGDATA_DIR}/postgresql.conf"
CONF_DIR="${PGDATA_DIR}/conf.d"
install -d -o postgres -g postgres -m 0700 "$CONF_DIR"

grep -qE "^[[:space:]]*include_dir[[:space:]]*=[[:space:]]*'conf.d'" "$CONF_MAIN" \
  || echo "include_dir = 'conf.d'" >> "$CONF_MAIN"

log "Writing managed settings to ${CONF_DIR}/10-cloudbolt.conf"
cat > "${CONF_DIR}/10-cloudbolt.conf" <<EOF
# Managed by CloudBolt - do not edit by hand
listen_addresses = '${LISTEN_ADDRESSES}'
port = ${PG_PORT}
max_connections = ${MAX_CONNECTIONS}
shared_buffers = '${SHARED_BUFFERS}'
password_encryption = 'scram-sha-256'
timezone = '${TIMEZONE}'
log_destination = 'stderr'
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%a.log'
log_line_prefix = '%m [%p] %q%u@%d '
log_min_duration_statement = 1000
EOF
chown postgres:postgres "${CONF_DIR}/10-cloudbolt.conf"
chmod 0600 "${CONF_DIR}/10-cloudbolt.conf"

# ---------------------------------------------------------------------------
# 7. pg_hba.conf - prepend a managed block (first match wins in pg_hba)
# ---------------------------------------------------------------------------
HBA="${PGDATA_DIR}/pg_hba.conf"
MARK_BEGIN="# BEGIN CloudBolt managed block"
MARK_END="# END CloudBolt managed block"

log "Applying managed pg_hba rules for ${CLIENT_CIDR}"
[[ -f "${HBA}.cb-orig" ]] || cp -p "$HBA" "${HBA}.cb-orig"

TMP_HBA="$(mktemp)"
{
  echo "$MARK_BEGIN"
  echo "# TYPE  DATABASE        USER            ADDRESS                 METHOD"
  echo "host    ${DB_NAME}      ${DB_OWNER}     127.0.0.1/32            scram-sha-256"
  echo "host    ${DB_NAME}      ${DB_OWNER}     ${CLIENT_CIDR}          scram-sha-256"
  echo "$MARK_END"
  # keep the original rules, minus any previous managed block
  sed "/^${MARK_BEGIN}$/,/^${MARK_END}$/d" "$HBA"
} > "$TMP_HBA"
install -o postgres -g postgres -m 0600 "$TMP_HBA" "$HBA"
rm -f "$TMP_HBA"

# ---------------------------------------------------------------------------
# 8. SELinux port label (non-default port only)
# ---------------------------------------------------------------------------
if [[ "$PG_PORT" != "5432" ]] && command -v semanage >/dev/null 2>&1 \
   && [[ "$(getenforce 2>/dev/null || echo Disabled)" != "Disabled" ]]; then
  if ! semanage port -l | awk '/^postgresql_port_t/ {print}' | grep -qw "$PG_PORT"; then
    log "Labeling TCP/${PG_PORT} as postgresql_port_t"
    semanage port -a -t postgresql_port_t -p tcp "$PG_PORT"
  fi
fi

# ---------------------------------------------------------------------------
# 9. Start the service
# ---------------------------------------------------------------------------
log "Enabling and starting ${PG_SERVICE}"
systemctl enable "$PG_SERVICE"
systemctl restart "$PG_SERVICE"

for _ in {1..30}; do
  as_postgres "${PG_BIN}/pg_isready" -q -p "$PG_PORT" && break
  sleep 2
done
as_postgres "${PG_BIN}/pg_isready" -p "$PG_PORT" >/dev/null \
  || die "PostgreSQL did not accept connections on port ${PG_PORT}. See journalctl -u ${PG_SERVICE}."

# ---------------------------------------------------------------------------
# 10. Role + database (idempotent; password passed as a psql variable so it is
#     properly quoted and never expanded into a shell-visible command line)
# ---------------------------------------------------------------------------
ROLE_EXISTS="$(as_postgres "$PSQL" -p "$PG_PORT" -tAc \
  "SELECT 1 FROM pg_roles WHERE rolname = '${DB_OWNER}'")"

if [[ "$ROLE_EXISTS" == "1" ]]; then
  log "Role '${DB_OWNER}' exists - updating password"
  as_postgres "$PSQL" -p "$PG_PORT" -q -v ON_ERROR_STOP=1 \
      -v role="$DB_OWNER" -v pw="$DB_OWNER_PASSWORD" <<'SQL'
ALTER ROLE :"role" WITH LOGIN PASSWORD :'pw';
SQL
else
  log "Creating role '${DB_OWNER}'"
  as_postgres "$PSQL" -p "$PG_PORT" -q -v ON_ERROR_STOP=1 \
      -v role="$DB_OWNER" -v pw="$DB_OWNER_PASSWORD" <<'SQL'
CREATE ROLE :"role" WITH LOGIN PASSWORD :'pw';
SQL
fi

DB_EXISTS="$(as_postgres "$PSQL" -p "$PG_PORT" -tAc \
  "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'")"

if [[ "$DB_EXISTS" == "1" ]]; then
  log "Database '${DB_NAME}' already exists - skipping create"
else
  log "Creating database '${DB_NAME}' owned by '${DB_OWNER}'"
  as_postgres "$PSQL" -p "$PG_PORT" -q -v ON_ERROR_STOP=1 \
      -v db="$DB_NAME" -v role="$DB_OWNER" <<'SQL'
CREATE DATABASE :"db" OWNER :"role" ENCODING 'UTF8';
SQL
fi

# Tighten the public schema so only the owner can create objects (PG15+ default
# already does this; explicit here so PG14 behaves the same way).
as_postgres "$PSQL" -p "$PG_PORT" -d "$DB_NAME" -q -v ON_ERROR_STOP=1 \
    -v role="$DB_OWNER" <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO :"role";
SQL

# ---------------------------------------------------------------------------
# 11. Firewall
# ---------------------------------------------------------------------------
if [[ "${OPEN_FIREWALL,,}" == "true" ]] && systemctl is-active --quiet firewalld; then
  log "Opening TCP/${PG_PORT} in firewalld"
  firewall-cmd --permanent --add-port="${PG_PORT}/tcp" >/dev/null
  firewall-cmd --reload >/dev/null
else
  log "Skipping firewalld configuration (disabled or firewalld not running)"
fi

# ---------------------------------------------------------------------------
# 12. Verify end-to-end over TCP as the application role
# ---------------------------------------------------------------------------
log "Verifying application login over TCP"
PG_VER="$(PGPASSWORD="$DB_OWNER_PASSWORD" "${PG_BIN}/psql" \
  -h 127.0.0.1 -p "$PG_PORT" -U "$DB_OWNER" -d "$DB_NAME" -tAc 'SHOW server_version')" \
  || die "Could not authenticate as ${DB_OWNER} to ${DB_NAME} on 127.0.0.1:${PG_PORT}"

cat <<EOF | tee -a "$LOG_FILE"

=========================================================================
 PostgreSQL deployment complete
=========================================================================
 Server version   : ${PG_VER}
 Service          : ${PG_SERVICE} ($(systemctl is-enabled "$PG_SERVICE"), $(systemctl is-active "$PG_SERVICE"))
 Data directory   : ${PGDATA_DIR}
 Listen / port    : ${LISTEN_ADDRESSES} : ${PG_PORT}
 Database         : ${DB_NAME}
 Owner role       : ${DB_OWNER}  (password set from CloudBolt parameter)
 Allowed clients  : 127.0.0.1/32, ${CLIENT_CIDR} (scram-sha-256)
 Connection string: postgresql://${DB_OWNER}@$(hostname -f 2>/dev/null || hostname):${PG_PORT}/${DB_NAME}
 Install log      : ${LOG_FILE}
=========================================================================
EOF

log "=== Finished successfully ==="
exit 0