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
        echo 'Deployment failed; restoring application files.' >&2
        rm -rf "$app/backend" "$app/frontend/dist"
        mv "$old_backend" "$app/backend"
        mv "$old_dist" "$app/frontend/dist"
        sudo -n /usr/bin/systemctl restart bc250-llm-client.service || true
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
    if curl --fail --silent --max-time 2 -H "X-BC250-Client: 1" http://127.0.0.1:8010/api/v1/conversations >/dev/null; then
        echo 'Deployment completed; API health check passed.'
        exit 0
    fi
    sleep 1
done
echo 'API health check failed.' >&2
exit 1
