#!/bin/sh
# Benchmark setup runs unattended. Preserve image-managed configuration files
# when a task installs or upgrades packages instead of blocking on a ucf prompt.
DEBIAN_FRONTEND=noninteractive UCF_FORCE_CONFFOLD=1 exec /usr/bin/apt-get "$@"
