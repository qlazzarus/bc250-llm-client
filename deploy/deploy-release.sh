#!/usr/bin/env bash
set -Eeuo pipefail

app=/opt/bc250-llm-client
archive="$app/.deploy-release.tar.gz"
stage=$(mktemp -d "$app/.deploy.XXXXXXXX")
old_backend="$stage/old-backend"
old_dist="$stage/old-dist"
installed=0

cleanup() {
    result=$?
    trap - EXIT
    if (( result != 0 && installed )); then
        # The new process may already have migrated or written the shared DB.
        # An old binary can ignore store namespaces, so it must not be restarted
        # automatically. Keep both code versions for a reviewed recovery.
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
[[ -f "$stage/backend/requirements.txt" && -f "$stage/frontend/dist/index.html" ]]
# Install dependencies before swapping files; the existing virtualenv stays in place.
"$app/.venv/bin/python" -m pip install -r "$stage/backend/requirements.txt"
# Confirm the narrowly scoped restart permission before replacing files.
sudo -n -l /usr/bin/systemctl restart bc250-llm-client.service >/dev/null
mv "$app/backend" "$old_backend"
if ! mv "$app/frontend/dist" "$old_dist"; then
    mv "$old_backend" "$app/backend"
    exit 1
fi
installed=1
mv "$stage/backend" "$app/backend"
mv "$stage/frontend/dist" "$app/frontend/dist"
sudo -n /usr/bin/systemctl restart bc250-llm-client.service
for attempt in {1..15}; do
    if curl --fail --silent --max-time 2 http://127.0.0.1:8010/api/v1/health >/dev/null; then
        echo 'Deployment completed; API health check passed.'
        exit 0
    fi
    sleep 1
done
echo 'API health check failed.' >&2
exit 1
