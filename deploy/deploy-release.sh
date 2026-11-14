#!/usr/bin/env bash
set -Eeuo pipefail

app=/opt/bc250-llm-client
archive="$app/.deploy-release.tar.gz"
state="$app/.deployed-components"
backend=${1:-true}
frontend=${2:-true}
backend_hash=${3:-}
frontend_hash=${4:-}
[[ "$backend" == true || "$backend" == false ]]
[[ "$frontend" == true || "$frontend" == false ]]
[[ "$backend" == true || "$frontend" == true ]]
if [[ -n "$backend_hash" || -n "$frontend_hash" ]]; then
    [[ "$backend_hash" =~ ^[0-9a-f]{40}$ && "$frontend_hash" =~ ^[0-9a-f]{40}$ ]]
fi
stage=$(mktemp -d "$app/.deploy.XXXXXXXX")
old_backend="$stage/old-backend"
old_dist="$stage/old-dist"
installed=0

cleanup() {
    result=$?
    trap - EXIT
    if (( result != 0 && installed )); then
        echo "Deployment failed after file replacement; automatic rollback is disabled." >&2
        echo "Previous files retained in $stage; an old or new process may still be running." >&2
        echo "Inspect and fence writers; preserve the database before recovery." >&2
        exit "$result"
    fi
    rm -rf "$stage"
    rm -f "$archive"
    exit "$result"
}
trap cleanup EXIT

[[ -f "$archive" && -d "$app/backend" && -d "$app/frontend/dist" && -x "$app/.venv/bin/python" ]]
tar -xzf "$archive" -C "$stage"
if [[ "$backend" == true ]]; then [[ -f "$stage/backend/requirements.txt" ]]; fi
if [[ "$frontend" == true ]]; then [[ -f "$stage/frontend/dist/index.html" ]]; fi
# An interrupted or failed deployment must force both components on the next run.
rm -f "$state"
if [[ "$backend" == true ]]; then
    "$app/.venv/bin/python" -m pip install -r "$stage/backend/requirements.txt"
    sudo -n -l /usr/bin/systemctl restart bc250-llm-client.service >/dev/null
    mv "$app/backend" "$old_backend"
    installed=1
    mv "$stage/backend" "$app/backend"
fi
if [[ "$frontend" == true ]]; then
    mv "$app/frontend/dist" "$old_dist"
    installed=1
    mv "$stage/frontend/dist" "$app/frontend/dist"
fi
if [[ "$backend" == true ]]; then
    sudo -n /usr/bin/systemctl restart bc250-llm-client.service
    healthy=false
    for attempt in {1..15}; do
        if curl --fail --silent --max-time 2 http://127.0.0.1:8010/api/v1/health >/dev/null; then healthy=true; break; fi
        sleep 1
    done
    if [[ "$healthy" != true ]]; then echo 'API health check failed.' >&2; exit 1; fi
fi
if [[ -n "$backend_hash" ]]; then
    printf '%s\n%s\n' "$backend_hash" "$frontend_hash" > "$stage/deployed-components"
    mv "$stage/deployed-components" "$state"
fi
echo 'Deployment completed.'
