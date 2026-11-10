#!/usr/bin/env bash
# Reviewable isolated root/systemd test. Changes only a fresh /run scratch tree
# and uniquely named transient units; never touches the production worker paths.
set -euo pipefail
source_dir=${1:?candidate source directory required}
deps_dir=${2:?staged worker dependency directory required}
[[ $EUID == 0 && $source_dir == /tmp/bc250-m602-* && $deps_dir == /tmp/bc250-worker-staging-test ]] || exit 2
[[ -f $source_dir/deploy/model-control/worker-bootstrap.py && -d $deps_dir/httpx ]] || exit 2
scratch=$(mktemp -d /run/bc250-m602-XXXXXXXX)
unit="bc250-m602-probe-${scratch##*-}"
echo "isolated_unit=$unit"
cleanup() {
    systemctl stop "$unit.socket" "$unit.service" "$unit-proc.service" >/dev/null 2>&1 || true
    rm -rf -- "$scratch"
}
trap cleanup EXIT
chmod 755 "$scratch"
mkdir -m 755 "$scratch/install" "$scratch/socket"
mkdir -m 750 "$scratch/state"
mkdir -m 700 "$scratch/credentials"
cp -a "$deps_dir" "$scratch/install/lib"
rm -rf -- "$scratch/install/lib/backend"
mkdir -p "$scratch/install/lib/backend/app"
touch "$scratch/install/lib/backend/__init__.py" "$scratch/install/lib/backend/app/__init__.py"
cp -a "$source_dir/backend/app/model_control" "$scratch/install/lib/backend/app/"
cp "$source_dir/deploy/model-control/worker-bootstrap.py" "$scratch/install/worker-bootstrap.py"
cp "$source_dir/scripts/m602-isolated-worker.py" "$source_dir/scripts/m602-proc-probe.py" "$scratch/"
find "$scratch/install" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "$scratch/install" -type f -name '*.pyc' -delete
chown -R root:root "$scratch"
chown root:ai-studio "$scratch/state"
chmod -R go-w "$scratch/install"
printf '{}' > "$scratch/state/switch.json"
touch "$scratch/gpu.lock" "$scratch/worker.lock"
chown root:ai-studio "$scratch/gpu.lock"
chmod 660 "$scratch/gpu.lock"
chown root:ai-studio "$scratch/state/switch.json"
chmod 600 "$scratch/worker.lock"
chmod 640 "$scratch/state/switch.json"
# Match the host policy's systemd-created file context before comparing labels.
restorecon -RF "$scratch/state"
runuser -u ai-studio -- /usr/bin/bash -c \
    'exec 9<> "$1"; flock -n 9; [[ $(cat "$2") == "{}" ]]' \
    _ "$scratch/gpu.lock" "$scratch/state/switch.json"
echo 'studio_user_can_lock_and_read_journal=True'
for key in gate-key studio-coordination-key llm-key; do
    printf '%s\n' 'isolated-test-key-0123456789abcdef' > "$scratch/credentials/$key"
    chmod 600 "$scratch/credentials/$key"
done
systemd-run --quiet --collect --unit="$unit" \
    --socket-property="ListenStream=$scratch/socket/control.sock" \
    --socket-property=SocketUser=root --socket-property=SocketGroup=yeppyshiba \
    --socket-property=SocketMode=0660 --socket-property=DirectoryMode=0755 \
    --property=User=root --property=Group=root --property=NoNewPrivileges=yes \
    --property=PrivateTmp=yes --property=ProtectHome=yes --property=ProtectSystem=strict \
    --property="ReadWritePaths=$scratch" --property=RestrictAddressFamilies='AF_UNIX AF_INET' \
    --property=CapabilityBoundingSet='CAP_CHOWN CAP_SYS_PTRACE' \
    --property="LoadCredential=gate-key:$scratch/credentials/gate-key" \
    --property="LoadCredential=studio-coordination-key:$scratch/credentials/studio-coordination-key" \
    --property="LoadCredential=llm-key:$scratch/credentials/llm-key" \
    --setenv=MODEL_CONTROL_SWITCH_ENABLED=false \
    /usr/bin/python3 -I -S "$scratch/m602-isolated-worker.py" "$scratch"
request() {
    runuser -u yeppyshiba -- /usr/bin/python3 - "$scratch/socket/control.sock" <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(15); s.connect(sys.argv[1])
s.sendall(b'{"operation":"submit","model":"test","idempotency_key":"m602"}\n')
data = b''
while not data.endswith(b'\n'):
    chunk = s.recv(4096)
    if not chunk: raise RuntimeError('worker closed socket')
    data += chunk
assert json.loads(data) == {'error': 'switch_disabled'}
print('real_client_uid_disabled_switch_rejected=True')
PY
}
request
runuser -u ai-studio -- /usr/bin/python3 - "$scratch/socket/control.sock" <<'PY'
import socket, sys
s = socket.socket(socket.AF_UNIX)
try:
    s.connect(sys.argv[1])
except PermissionError:
    print('unrelated_user_socket_access_denied=True')
else:
    raise RuntimeError('unrelated user could access worker socket')
PY
first_pid=$(systemctl show "$unit.service" -p MainPID --value)
[[ $first_pid =~ ^[1-9][0-9]*$ ]]
systemctl stop "$unit.service"
request
second_pid=$(systemctl show "$unit.service" -p MainPID --value)
[[ $second_pid =~ ^[1-9][0-9]*$ && $first_pid != "$second_pid" ]]
echo 'systemd_socket_activation_and_restart=True'
systemd-run --quiet --wait --pipe --collect --unit="$unit-proc" \
    --property=User=root --property=Group=root --property=NoNewPrivileges=yes \
    --property=PrivateTmp=yes --property=ProtectHome=yes --property=ProtectSystem=strict \
    --property="ReadWritePaths=$scratch" --property=RestrictAddressFamilies='AF_UNIX AF_INET' \
    --property=CapabilityBoundingSet='CAP_CHOWN CAP_SYS_PTRACE' \
    /usr/bin/python3 -I -S "$scratch/m602-proc-probe.py" "$scratch"
echo 'isolated_root_probe_complete=True'
