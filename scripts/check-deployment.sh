#!/usr/bin/env bash
# Read-only readiness check; this script never deploys or restarts services.
set -euo pipefail
url=${1:-https://battute.duckdns.org}
health=$(curl --fail --silent --show-error --max-time 20 "$url/api/health")
python3 -c 'import json,sys; h=json.loads(sys.argv[1]); assert h["status"]=="ok" and h["workerOnline"] and h["cleanupOnline"], h; print("API, worker e pulizia raggiungibili.")' "$health"
curl --fail --silent --show-error --max-time 20 --output /dev/null "$url/"
printf 'Frontend raggiungibile tramite %s\n' "$url"
