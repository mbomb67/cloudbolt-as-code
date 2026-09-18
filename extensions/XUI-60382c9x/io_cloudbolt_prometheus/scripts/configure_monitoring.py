from common.methods import set_progress
from infrastructure.models import Server
from tags.models import CloudBoltTag
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


VALID_OS_FAMILIES = ["Red Hat", "CentOS", "Oracle Enterprise Linux",
                     "Amazon Linux", "Ubuntu", "Windows"]


def run(job, server=None, *args, **kwargs):
    servers = kwargs.get("servers", [])
    if not servers:
        servers = [server]
    if not servers:
        return "FAILURE", "", "No Servers were detected"
    for server in servers:
        set_progress(f"Starting {server.hostname}")
        os_family = server.os_family.name
        if os_family not in VALID_OS_FAMILIES:
            set_progress(f"OS family {os_family} is not supported. Skipping")
            return "", "", ""
        # Install node_exporter on the server
        if os_family == "Ubuntu":
            script = get_ubuntu_script()
        elif os_family == "Windows":
            script = get_windows_script()
        else:
            script = get_rhel_script()
        server.execute_script(script_contents=script, run_with_sudo=True)

        # Set the "monitor" label on the server
        tag, _ = CloudBoltTag.objects.get_or_create(
            name="monitor",
        )
        server.tags.add(tag)
    return "SUCCESS", "", ""


def get_rhel_script():
    """
    This function returns the script to be used for RHEL compliant systems
    e.g. RHEL 7, RHEL 8, RHEL 9, CentOS 7, CentOS 8, CentOS 9, Oracle Linux 7,
    Oracle Linux 8, Oracle Linux 9
    """
    return """#!/bin/bash
set -euo pipefail

IP_ADDR='{{ server.nics.first.private_ip }}'
VERSION=1.9.1

case "$(uname -m)" in
  x86_64) NODE_ARCH=amd64 ;;
  aarch64) NODE_ARCH=arm64 ;;
  armv7l) NODE_ARCH=armv7 ;;
  *)
    echo "Unsupported architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

TARBALL="node_exporter-${VERSION}.linux-${NODE_ARCH}.tar.gz"
EXTRACT_DIR="node_exporter-${VERSION}.linux-${NODE_ARCH}"

if ! id -u node_exporter &>/dev/null; then
  useradd --no-create-home --shell /bin/false node_exporter
fi

mkdir -p /var/lib/node_exporter/textfile_collector
chown node_exporter:node_exporter /var/lib/node_exporter/textfile_collector

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"

curl -fSL "https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/${TARBALL}" -o "$TARBALL"
tar -xzf "$TARBALL"
cp "${EXTRACT_DIR}/node_exporter" /usr/local/bin/node_exporter
chown node_exporter:node_exporter /usr/local/bin/node_exporter

tee /etc/systemd/system/node_exporter.service > /dev/null <<EOF
[Unit]
Description=Prometheus Node Exporter
Wants=network-online.target
After=network-online.target

[Service]
User=node_exporter
Group=node_exporter
Type=simple
EnvironmentFile=/etc/sysconfig/node_exporter
ExecStart=/usr/local/bin/node_exporter \$OPTIONS

[Install]
WantedBy=default.target
EOF

# update /etc/sysconfig/node_exporter
echo "OPTIONS='--collector.textfile.directory /var/lib/node_exporter/textfile_collector --collector.cpu.info --collector.processes --web.listen-address=${IP_ADDR}:9100'" > /etc/sysconfig/node_exporter

if test -f /etc/default/prometheus-node-exporter; then
	cp /etc/sysconfig/node_exporter /etc/default/prometheus-node-exporter
fi

systemctl daemon-reload
systemctl enable node_exporter
systemctl restart node_exporter

if ! systemctl is-active --quiet node_exporter; then
  echo "node_exporter failed to start" >&2
  systemctl status node_exporter --no-pager || true
  journalctl -u node_exporter -n 30 --no-pager >&2 || true
  exit 1
fi

if command -v firewall-cmd &> /dev/null; then
  echo "Configuring firewalld rules for node_exporter..."

  sudo firewall-cmd --add-port=9100/tcp --permanent
  sudo firewall-cmd --reload

  echo "Firewalld rules updated."
else
  echo "firewalld not installed. Skipping firewall rule configuration."
fi

echo "Prometheus node_exporter installed and bound to ${IP_ADDR}:9100."
"""


def get_ubuntu_script():
    """
    This function returns the script to be used for Ubuntu compliant systems
    """
    return """#!/bin/bash
set -euo pipefail

IP_ADDR='{{ server.nics.first.private_ip }}'
VERSION=1.9.1

case "$(uname -m)" in
  x86_64) NODE_ARCH=amd64 ;;
  aarch64) NODE_ARCH=arm64 ;;
  armv7l) NODE_ARCH=armv7 ;;
  *)
    echo "Unsupported architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

TARBALL="node_exporter-${VERSION}.linux-${NODE_ARCH}.tar.gz"
EXTRACT_DIR="node_exporter-${VERSION}.linux-${NODE_ARCH}"

if ! id -u node_exporter &>/dev/null; then
  useradd --no-create-home --shell /bin/false node_exporter
fi

mkdir -p /var/lib/node_exporter/textfile_collector
chown node_exporter:node_exporter /var/lib/node_exporter/textfile_collector

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"

curl -fSL "https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/${TARBALL}" -o "$TARBALL"
tar -xzf "$TARBALL"
cp "${EXTRACT_DIR}/node_exporter" /usr/local/bin/node_exporter
chown node_exporter:node_exporter /usr/local/bin/node_exporter

tee /etc/systemd/system/node_exporter.service > /dev/null <<EOF
[Unit]
Description=Prometheus Node Exporter
Wants=network-online.target
After=network-online.target

[Service]
User=node_exporter
Group=node_exporter
Type=simple
EnvironmentFile=/etc/default/node_exporter
ExecStart=/usr/local/bin/node_exporter \$OPTIONS

[Install]
WantedBy=default.target
EOF

# update /etc/default/node_exporter
echo "OPTIONS='--collector.textfile.directory /var/lib/node_exporter/textfile_collector --collector.cpu.info --collector.processes --web.listen-address=${IP_ADDR}:9100'" > /etc/default/node_exporter

systemctl daemon-reload
systemctl enable node_exporter
systemctl restart node_exporter

if ! systemctl is-active --quiet node_exporter; then
  echo "node_exporter failed to start" >&2
  systemctl status node_exporter --no-pager || true
  journalctl -u node_exporter -n 30 --no-pager >&2 || true
  exit 1
fi

if command -v ufw &> /dev/null; then
  echo "Configuring UFW rules for node_exporter..."
  ufw allow 9100/tcp
  echo "UFW rules updated."
else
  echo "ufw not installed. Skipping firewall rule configuration."
fi

echo "Prometheus node_exporter installed and bound to ${IP_ADDR}:9100."
"""


def get_windows_script():
    return """
# Get latest release and download URL dynamically
$latest = Invoke-RestMethod -Uri "https://api.github.com/repos/prometheus-community/windows_exporter/releases/latest"
$msiUrl = ($latest.assets | Where-Object { $_.name -like "*amd64.msi" }).browser_download_url
$msiPath = "$env:TEMP\windows_exporter.msi"

Write-Host "Downloading windows_exporter $($latest.tag_name)..."
Invoke-WebRequest -Uri $msiUrl -OutFile $msiPath

# Verify download looks valid
$fileSize = (Get-Item $msiPath).Length
Write-Host "Downloaded $([math]::Round($fileSize / 1MB, 2)) MB"
if ($fileSize -lt 1MB) { Write-Error "File too small — download likely failed"; exit 1 }

# Install with logging
$logPath = "$env:TEMP\windows_exporter_install.log"
Write-Host "Installing..."
Start-Process msiexec.exe -Wait -ArgumentList "/i `"$msiPath`" /qn /l*v `"$logPath`""

# Check for install errors
$errors = Get-Content $logPath | Select-String -Pattern "error|fail|return value 3" -CaseSensitive:$false
if ($errors) { Write-Warning "Potential install issues:"; $errors }

# Open firewall port 9182
Write-Host "Configuring firewall..."
New-NetFirewallRule -DisplayName "Prometheus Windows Exporter" `
    -Direction Inbound -LocalPort 9182 -Protocol TCP -Action Allow `
    -ErrorAction SilentlyContinue

# Verify service is running
$svc = Get-Service | Where-Object { $_.Name -like "*exporter*" }
if ($svc) {
    Write-Host "Service '$($svc.Name)' status: $($svc.Status)"
} else {
    Write-Error "Service not found — check install log at $logPath"
}
"""
