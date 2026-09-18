#!/usr/bin/env bash
#
# CloudBolt catalog item post-provision script
# "Linux Web Server (nginx)" - Oracle Linux 8
#
# Installs nginx, writes a deterministic nginx.conf plus a single site config,
# publishes a landing page that identifies the server and the order that built
# it, and verifies the site responds before reporting success.
#
# Idempotent: safe to re-run on the same server.
#
# Notes for CloudBolt integration:
#   - Every variable reads from the environment first, so values can come from
#     parameter substitution or be exported by a plugin.
#   - The final block prints SITE_URL - map that to a server/resource attribute
#     so the resource detail page can render it as a clickable link.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# VARIABLES  (override via CloudBolt parameters / environment)
# ---------------------------------------------------------------------------
SITE_NAME="{{server.nginx_site_name}}"
SITE_PORT="{{server.nginx_site_port}}"
SITE_ROOT="/var/www/cloudbolt-site"
SERVER_NAME="_"                  # '_' matches any Host header

# Shown on the landing page - wire these to order metadata
ORDER_ID="n/a"
REQUESTED_BY="{{server.owner}}"
ENVIRONMENT_NAME="{{server.environment.name}}"
OWNER_GROUP="{{server.group.name}}"

NGINX_STREAM="1.24"                 # e.g. '1.22'; empty = repo default
WORKER_CONNECTIONS="1024"
OPEN_FIREWALL="true"
LOG_FILE="/var/log/cb_nginx_install.log"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { printf '%s [INFO ] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE"; }
warn() { printf '%s [WARN ] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE" >&2; }
die()  { printf '%s [ERROR] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE" >&2; exit 1; }
trap 'die "Failed at line $LINENO"' ERR
 
# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
touch "$LOG_FILE" 2>/dev/null || LOG_FILE=/dev/null
log "=== nginx install starting on $(hostname -f 2>/dev/null || hostname) ==="
[[ "$(id -u)" -eq 0 ]] || die "This script must run as root."
 
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  log "Detected OS: ${PRETTY_NAME:-unknown}"
  [[ "${VERSION_ID:-}" == 8* ]] || warn "Expected EL8; found '${VERSION_ID:-none}'. Continuing."
fi
 
# ---------------------------------------------------------------------------
# 2. Install nginx
# ---------------------------------------------------------------------------
if rpm -q nginx >/dev/null 2>&1; then
  log "nginx already installed: $(nginx -v 2>&1)"
else
  if [[ -n "$NGINX_STREAM" ]]; then
    log "Enabling nginx module stream ${NGINX_STREAM}"
    dnf -y module reset nginx
    dnf -y module enable "nginx:${NGINX_STREAM}"
  fi
  log "Installing nginx"
  dnf install -y nginx
fi
 
# Needed if we relabel a custom docroot or use a non-standard port
if [[ "$(getenforce 2>/dev/null || echo Disabled)" != "Disabled" ]]; then
  rpm -q policycoreutils-python-utils >/dev/null 2>&1 || dnf install -y policycoreutils-python-utils
fi
 
# ---------------------------------------------------------------------------
# 3. Document root
# ---------------------------------------------------------------------------
log "Preparing document root ${SITE_ROOT}"
install -d -o root -g root -m 0755 "$SITE_ROOT"
 
if [[ "$(getenforce 2>/dev/null || echo Disabled)" != "Disabled" ]]; then
  semanage fcontext -a -t httpd_sys_content_t "${SITE_ROOT}(/.*)?" 2>/dev/null || true
  restorecon -R "$SITE_ROOT"
  if [[ "$SITE_PORT" != "80" && "$SITE_PORT" != "443" ]]; then
    # The httpd domain may already bind this port under http_port_t OR
    # http_cache_port_t (8080, 8118, 8123... ship as http_cache_port_t), in
    # which case no change is needed. If some other type owns the port,
    # 'semanage port -a' fails with "already defined" - use -m to remap.
    if semanage port -l | awk '/^http_port_t|^http_cache_port_t/' | grep -qw "$SITE_PORT"; then
      log "TCP/${SITE_PORT} is already labeled for HTTP use - no SELinux change needed"
    elif semanage port -a -t http_port_t -p tcp "$SITE_PORT" 2>/dev/null; then
      log "Labeled TCP/${SITE_PORT} as http_port_t"
    elif semanage port -m -t http_port_t -p tcp "$SITE_PORT" 2>/dev/null; then
      log "Remapped TCP/${SITE_PORT} to http_port_t"
    else
      warn "Could not label TCP/${SITE_PORT} for HTTP. If nginx fails to bind, check: semanage port -l | grep ${SITE_PORT}"
    fi
  fi
fi
 
# ---------------------------------------------------------------------------
# 4. Landing page
# ---------------------------------------------------------------------------
PRIMARY_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
FQDN="$(hostname -f 2>/dev/null || hostname)"
BUILD_TIME="$(date '+%Y-%m-%d %H:%M:%S %Z')"
OS_PRETTY="${PRETTY_NAME:-Linux}"
NGINX_VER="$(nginx -v 2>&1 | sed 's|nginx version: ||')"
 
log "Writing landing page to ${SITE_ROOT}/index.html"
cat > "${SITE_ROOT}/index.html" <<HTML
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${SITE_NAME}</title>
  <style>
    :root { color-scheme: light dark; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center;
           font-family: ui-sans-serif, system-ui, "Segoe UI", Helvetica, Arial, sans-serif;
           background: #0f1b2d; color: #e8edf5; }
    .card { width: min(680px, 90vw); background: #16243b; border: 1px solid #24374f;
            border-radius: 14px; padding: 2.25rem 2.5rem;
            box-shadow: 0 18px 50px rgba(0,0,0,.35); }
    .status { display: inline-flex; align-items: center; gap: .5rem;
              font-size: .8rem; letter-spacing: .08em; text-transform: uppercase;
              color: #7ee2a8; margin-bottom: 1rem; }
    .dot { width: .6rem; height: .6rem; border-radius: 50%; background: #37d67a; }
    h1 { margin: 0 0 .35rem; font-size: 1.7rem; }
    p.lede { margin: 0 0 1.75rem; color: #9fb0c8; font-size: .95rem; }
    table { width: 100%; border-collapse: collapse; font-size: .92rem; }
    th, td { text-align: left; padding: .55rem .25rem; border-bottom: 1px solid #24374f; }
    th { color: #9fb0c8; font-weight: 500; width: 42%; }
    td { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    tr:last-child th, tr:last-child td { border-bottom: none; }
    footer { margin-top: 1.75rem; font-size: .8rem; color: #7c8ea8; }
  </style>
</head>
<body>
  <main class="card">
    <div class="status"><span class="dot"></span> Service online</div>
    <h1>${SITE_NAME}</h1>
    <p class="lede">This nginx instance was provisioned and configured by CloudBolt.</p>
    <table>
      <tr><th>Hostname</th><td>${FQDN}</td></tr>
      <tr><th>IP address</th><td>${PRIMARY_IP:-unknown}</td></tr>
      <tr><th>Operating system</th><td>${OS_PRETTY}</td></tr>
      <tr><th>Web server</th><td>nginx ${NGINX_VER}</td></tr>
      <tr><th>Listening port</th><td>${SITE_PORT}</td></tr>
      <tr><th>Order ID</th><td>${ORDER_ID}</td></tr>
      <tr><th>Requested by</th><td>${REQUESTED_BY}</td></tr>
      <tr><th>Owner group</th><td>${OWNER_GROUP}</td></tr>
      <tr><th>Environment</th><td>${ENVIRONMENT_NAME}</td></tr>
      <tr><th>Configured at</th><td>${BUILD_TIME}</td></tr>
    </table>
    <footer>Health endpoint: <code>/health</code></footer>
  </main>
</body>
</html>
HTML
 
chmod 0644 "${SITE_ROOT}/index.html"
[[ "$(getenforce 2>/dev/null || echo Disabled)" == "Disabled" ]] || restorecon "${SITE_ROOT}/index.html"
 
# ---------------------------------------------------------------------------
# 5. nginx configuration
#    The stock EL nginx.conf defines its own default server on :80, which would
#    shadow this site. Replace it with a minimal deterministic config that only
#    includes conf.d, and keep the original as a backup.
# ---------------------------------------------------------------------------
if [[ ! -f /etc/nginx/nginx.conf.cb-orig ]]; then
  cp -p /etc/nginx/nginx.conf /etc/nginx/nginx.conf.cb-orig
  log "Backed up stock nginx.conf to /etc/nginx/nginx.conf.cb-orig"
fi
 
log "Writing /etc/nginx/nginx.conf"
cat > /etc/nginx/nginx.conf <<CONF
# Managed by CloudBolt - original saved as nginx.conf.cb-orig
user                 nginx;
worker_processes     auto;
error_log            /var/log/nginx/error.log notice;
pid                  /run/nginx.pid;
 
events {
    worker_connections ${WORKER_CONNECTIONS};
}
 
http {
    include             /etc/nginx/mime.types;
    default_type        application/octet-stream;
 
    log_format  main    '\$remote_addr - \$remote_user [\$time_local] "\$request" '
                        '\$status \$body_bytes_sent "\$http_referer" '
                        '"\$http_user_agent" "\$http_x_forwarded_for"';
    access_log          /var/log/nginx/access.log main;
 
    sendfile            on;
    tcp_nopush          on;
    keepalive_timeout   65;
    server_tokens       off;
 
    include             /etc/nginx/conf.d/*.conf;
}
CONF
 
log "Writing /etc/nginx/conf.d/cloudbolt-site.conf"
cat > /etc/nginx/conf.d/cloudbolt-site.conf <<CONF
# Managed by CloudBolt
server {
    listen       ${SITE_PORT} default_server;
    listen       [::]:${SITE_PORT} default_server;
    server_name  ${SERVER_NAME};
    root         ${SITE_ROOT};
    index        index.html;
 
    location / {
        try_files \$uri \$uri/ =404;
    }
 
    location = /health {
        access_log off;
        add_header Content-Type text/plain;
        return 200 "ok\n";
    }
}
CONF
 
log "Validating nginx configuration"
nginx -t 2>&1 | tee -a "$LOG_FILE" || die "nginx configuration test failed."
 
# ---------------------------------------------------------------------------
# 6. Service + firewall
# ---------------------------------------------------------------------------
log "Enabling and starting nginx"
systemctl enable nginx
systemctl restart nginx
 
if [[ "${OPEN_FIREWALL,,}" == "true" ]] && systemctl is-active --quiet firewalld; then
  log "Opening TCP/${SITE_PORT} in firewalld"
  firewall-cmd --permanent --add-port="${SITE_PORT}/tcp" >/dev/null
  firewall-cmd --reload >/dev/null
else
  log "Skipping firewalld configuration (disabled or firewalld not running)"
fi
 
# ---------------------------------------------------------------------------
# 7. Verify
# ---------------------------------------------------------------------------
log "Verifying the site responds locally"
HTTP_CODE=""
for _ in {1..15}; do
  HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${SITE_PORT}/" || true)"
  [[ "$HTTP_CODE" == "200" ]] && break
  sleep 2
done
[[ "$HTTP_CODE" == "200" ]] || die "Site returned HTTP ${HTTP_CODE:-no response} on port ${SITE_PORT}."
 
HEALTH="$(curl -sf "http://127.0.0.1:${SITE_PORT}/health" || echo 'FAILED')"
[[ "$HEALTH" == "ok" ]] || warn "Health endpoint returned '${HEALTH}'."
 
SITE_URL="http://${FQDN}$( [[ "$SITE_PORT" == "80" ]] || printf ':%s' "$SITE_PORT" )/"
 
cat <<EOF | tee -a "$LOG_FILE"
 
=========================================================================
 Linux web server deployment complete
=========================================================================
 SITE_URL       : ${SITE_URL}
 By IP          : http://${PRIMARY_IP:-unknown}$( [[ "$SITE_PORT" == "80" ]] || printf ':%s' "$SITE_PORT" )/
 Health check   : ${SITE_URL}health
 nginx version  : ${NGINX_VER}
 Document root  : ${SITE_ROOT}
 Service        : nginx ($(systemctl is-enabled nginx), $(systemctl is-active nginx))
 Local HTTP test: ${HTTP_CODE}
 Install log    : ${LOG_FILE}
=========================================================================
EOF
 
log "=== Finished successfully ==="
exit 0