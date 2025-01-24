#!/bin/bash

# Generate ansible inventory from running docker containers
INVENTORY_FILE="inventory.ini"

echo "[docker_nodes]" > $INVENTORY_FILE

# Get container IPs and format as ansible inventory
docker compose ps --format '{{.Names}}' | while read -r container; do
    ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $container)
    echo "$container ansible_host=$ip >> $INVENTORY_FILE
done

echo "Generated inventory file: $INVENTORY_FILE"
