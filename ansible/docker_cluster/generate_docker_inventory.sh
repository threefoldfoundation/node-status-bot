#!/bin/bash

# Generate docker.ini from docker compose ps output
OUTPUT_FILE="docker.ini"

# Write the header
echo "[docker]" > "$OUTPUT_FILE"

# Process the docker compose output
docker compose ps | awk 'NR>1 {print $1 " ansible_connection=docker service_host=" $1}' >> "$OUTPUT_FILE"

echo "Generated $OUTPUT_FILE"
