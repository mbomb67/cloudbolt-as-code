# Prometheus Monitoring Extension

## Installation

### Overview
1. Install/Configure Prometheus Server
2. Install Monitoring XUI in CloudBolt
4. Install Prometheus Node Exporter Orchestration Action

### Install Prometheus Server

#### Installation

```
curl -s https://packagecloud.io/install/repositories/prometheus-rpm/release/script.rpm.sh | sudo bash
yum install prometheus2
```

#### Configuration
When installed, the Monitoring XUI provides Prometheus with a list of nodes to scan through an API call to: `https://{CLOUDBOLT_SERVER}/xui/io_cloudbolt_prometheus/api/targets/`.

Update `/etc/prometheus/prometheus.yml`, setting the appropriate CloudBolt server URL in the last line:
```
global:
  scrape_interval:     15s # Set the scrape interval to every 15 seconds. Default is every 1 minute.
  evaluation_interval: 15s # Evaluate rules every 15 seconds. The default is every 1 minute.
  # scrape_timeout is set to the global default (10s).

# Alertmanager configuration
alerting:
  alertmanagers:
  - static_configs:
    - targets:
      # - alertmanager:9093

# Load rules once and periodically evaluate them according to the global 'evaluation_interval'.
rule_files:
  # - "first_rules.yml"
  # - "second_rules.yml"

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
    - targets: ['localhost:9090']

  - job_name: 'cloudbolt'
    http_sd_configs:
    - url: 'https://{YOUR CB SERVER HOSTNAME}/xui/io_cloudbolt_prometheus/api/targets/'
```

NOTE: Prometheus Server MUST have network line-of-sight to monitored servers in order to scrape metrics.

### Install Monitoring XUI
The contents of this repository should be copied to `/var/opt/cloudbolt/proserv/xui/io_cloudbolt_prometheus` or uploaded via Admin/Extensions.

Once copied run: `/opt/cloudbolt/manage.py collectstatic --noinput`, or click "Collect Static Assets" on the Admin/Extensions Management page, and restart httpd.

### Deploy Orchestration Action
The orchestration action at `scripts/install_node_exporter.sh` should be deployed at Post-Provision or as a Blueprint action item. It will install and configure Node Exporter on target Yum/dnf-based VMs:
