#!/bin/bash
IP_ADDR='{{ server.nics.first.private_ip }}'
yum install -y golang-github-prometheus-node-exporter

# update /etc/sysconfig/node_exporter
echo "OPTIONS='--collector.textfile.directory /var/lib/node_exporter/textfile_collector --collector.cpu.info --collector.processes --web.listen-address=${IP_ADDR}:9100'" > /etc/sysconfig/node_exporter

if test -f /etc/default/prometheus-node-exporter; then
	cp /etc/sysconfig/node_exporter /etc/default/prometheus-node-exporter
fi


systemctl enable node_exporter
systemctl restart node_exporter

echo "Prometheus node_exporter installed and bound to ${IP_ADDR}:9100."

