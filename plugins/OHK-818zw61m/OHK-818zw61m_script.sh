#!/bin/bash
# POC Hello World (Linux): proves that CloudBolt runs a script inside the
# guest over SSH without an agent (test plan 1.1.3, 4.1.2, 4.3.3).
set -e
echo "Hello from CloudBolt on $(hostname) at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Running as $(whoami) on $(uname -sr)"
echo "poc_hello_world=ok"
