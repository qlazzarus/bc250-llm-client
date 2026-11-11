"""Deployment preparation fails closed without altering existing state."""
import json
import os
import shutil
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import migrate_legacy_gate as migration
import studio_quiescence_status as studio
import coordination_status as coordination
import prepare_coordination_credentials as credentials
import rollback_readiness as rollback
import restore_legacy_pause as restore
import release_startup_hold as startup
import create_rollout_backup as backup


@pytest.mark.parametrize('paused', [False, True])
def test_drained_snapshot_provisions_exclusive_startup_hold(tmp_path, monkeypatch, paused):
    snapshot = {'pid': 123, 'paused': paused, 'active_requests': 0, 'waiting_requests': 0}
    path = tmp_path / 'snapshot.json'
    migration.save_snapshot(path, snapshot)
    assert json.loads(path.read_text()) == snapshot
    monkeypatch.setattr(migration, 'read_snapshot', lambda _: snapshot)
    monkeypatch.setattr(migration, 'require_stopped', lambda: None)
    monkeypatch.setattr(migration.os, 'chown', lambda *args: None)
    monkeypatch.setattr(migration.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=1))
    monkeypatch.setattr(migration.grp, 'getgrnam', lambda _: SimpleNamespace(gr_gid=1))
    state = tmp_path / 'state.json'
    assert migration.initialize(path, state) is paused
    assert set(json.loads(state.read_text())['holds']) == ({'startup', 'legacy'} if paused else {'startup'})
    with pytest.raises(FileExistsError): migration.initialize(path, state)


@pytest.mark.parametrize('field,value', [('paused', 0), ('active_requests', 1), ('waiting_requests', 1), ('pid', True)])
def test_snapshot_rejects_invalid_or_undrained_state(tmp_path, field, value):
    snapshot = {'pid': 123, 'paused': False, 'active_requests': 0, 'waiting_requests': 0}
    snapshot[field] = value
    with pytest.raises(ValueError): migration.save_snapshot(tmp_path / 'snapshot', snapshot)
    assert not (tmp_path / 'snapshot').exists()


@pytest.mark.parametrize('suffix', ['', '.lock', '.uncertain'])
def test_existing_or_dangling_state_marker_blocks_initialization(tmp_path, monkeypatch, suffix):
    state = tmp_path / 'state.json'
    Path(str(state) + suffix).symlink_to(tmp_path / 'missing')
    monkeypatch.setattr(migration, 'read_snapshot', lambda _: {'paused': False, 'active_requests': 0, 'waiting_requests': 0})
    monkeypatch.setattr(migration, 'require_stopped', lambda: None)
    with pytest.raises(FileExistsError): migration.initialize('unused', state)


def test_gate_restart_during_initialization_leaves_state_for_investigation(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, 'read_snapshot', lambda _: {'paused': True, 'active_requests': 0, 'waiting_requests': 0})
    calls = iter([None, RuntimeError('gate restarted')])
    def check():
        value = next(calls)
        if value: raise value
    monkeypatch.setattr(migration, 'require_stopped', check)
    monkeypatch.setattr(migration.os, 'chown', lambda *args: None)
    monkeypatch.setattr(migration.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=1))
    monkeypatch.setattr(migration.grp, 'getgrnam', lambda _: SimpleNamespace(gr_gid=1))
    state = tmp_path / 'state.json'
    with pytest.raises(RuntimeError, match='restarted'): migration.initialize('unused', state)
    assert set(json.loads(state.read_text())['holds']) == {'legacy', 'startup'}


def test_studio_quiescence_requires_all_gpu_and_recovery_fields():
    status = {'recovery_required': False, 'scheduler_running': True, 'gpu_owner': None,
              'active_job': None, 'transition': {}, 'llm_recovery': {}}
    assert studio.validate_status(status)
    for key, value in [('recovery_required', True), ('scheduler_running', False), ('gpu_owner', 'image'),
                       ('active_job', 'job'), ('transition', {'stage': 'drain'}), ('llm_recovery', {'pending': True})]:
        assert not studio.validate_status({**status, key: value})
    del status['gpu_owner']
    with pytest.raises(RuntimeError): studio.validate_status(status)


def test_studio_job_check_rejects_unknown_status_and_changed_snapshots(monkeypatch):
    monkeypatch.setattr(studio, 'request', lambda *_: [{'job_id': 'one', 'status': 'unknown'}])
    with pytest.raises(RuntimeError): studio.jobs('test-key')
    monkeypatch.setattr(studio, 'identity', lambda: (123, 'start'))
    monkeypatch.setattr(studio, 'read_key', lambda _: 'test-key')
    monkeypatch.setattr(studio, 'request', lambda *_: {'recovery_required': False, 'scheduler_running': True,
                            'gpu_owner': None, 'active_job': None, 'transition': {}, 'llm_recovery': {}})
    snapshots = iter([[('one', 'queued')], [('one', 'completed')]])
    monkeypatch.setattr(studio, 'jobs', lambda _: next(snapshots))
    with pytest.raises(RuntimeError): studio.read_status()


def test_gate_status_requires_drained_durable_unreserved_protocol3():
    gate = {'protocol': 3, 'durable': True, 'epoch': 'a' * 32, 'holds': ['startup'], 'paused': True,
            'active_requests': 0, 'waiting_requests': 0, 'recovery_exclusive_hold': None,
            'deployment_generation': None}
    assert coordination.valid_gate(gate)
    for name, value in [('protocol', True), ('durable', False), ('epoch', 'bad'), ('paused', False),
                        ('active_requests', True), ('waiting_requests', 1),
                        ('recovery_exclusive_hold', 'switch-1'), ('deployment_generation', 'pending')]:
        assert not coordination.valid_gate({**gate, name: value})


def test_private_credential_bundle_preserves_existing_operator_and_never_overwrites(tmp_path):
    dest = tmp_path / 'bundle'
    assert credentials.prepare(dest, {b'OPERATOR_KEY': b'prior-operator'},
                               {b'STUDIO_API_KEY': b'existing-api', b'STUDIO_ADMIN_KEY': b'existing-admin'}, '')
    assert dest.stat().st_mode & 0o777 == 0o700
    assert all(item.stat().st_mode & 0o777 == 0o600 for item in dest.iterdir())
    assert 'OPERATOR_KEY="prior-operator"' in (dest / 'client.env').read_text()
    assert (dest / 'llm-key').read_text() == ''
    assert len({(dest / name).read_text() for name in ('gate-admin-key', 'gate-client-key',
               'gate-studio-key', 'switch-gate-key', 'studio-coordination-key')}) == 5
    assert (dest / 'gate-client-key').read_text().strip() in (dest / 'client.env').read_text()
    assert (dest / 'gate-studio-key').read_text().strip() in (dest / 'studio.env').read_text()
    with pytest.raises(FileExistsError): credentials.prepare(dest, {}, {}, '')


def test_environment_fragment_rejects_control_characters():
    with pytest.raises(ValueError): credentials.env_line('KEY', 'secret\nOTHER=oops')


def test_activation_fragments_override_only_explicit_opt_in():
    base = ROOT / 'deploy/model-control'
    client_dir = base / 'bc250-llm-client.service.d'
    initial = client_dir / 'model-control.conf'
    enable = client_dir / 'zz-model-control-enable.conf'
    assert initial.name < enable.name
    assert 'EnvironmentFile=/etc/bc250-model-control/client.env' in initial.read_text()
    assert 'EnvironmentFile=/etc/bc250-model-control/client-activation.env' in enable.read_text()
    assert (base / 'client-activation.env').read_text().strip() == 'MODEL_CONTROL_ENABLED=true'
    assert 'MODEL_CONTROL_SWITCH_ENABLED=false' in (base / 'bc250-model-worker.service').read_text()
    assert 'Environment=MODEL_CONTROL_SWITCH_ENABLED=true' in (base / 'bc250-model-worker.service.d/zz-switch-enable.conf').read_text()


def test_rollback_requires_empty_and_readable_worker_records():
    worker = {'operation': None, 'hold_id': None, 'journal': {}, 'journal_error': None, 'reconciliation_error': None}
    assert rollback.validate_worker(worker)
    for name, value in [('operation', {'id': 'pending'}), ('hold_id', 'switch-x'), ('journal', {'stage': 'pausing'}),
                        ('journal_error', 'invalid'), ('reconciliation_error', 'mismatch')]:
        assert not rollback.validate_worker({**worker, name: value})
    del worker['journal_error']
    assert not rollback.validate_worker(worker)


def test_restore_legacy_pause_does_not_change_already_paused_gate(monkeypatch):
    monkeypatch.setattr(restore, 'service_identity', lambda: (123, 'start'))
    monkeypatch.setattr(restore, 'read_status', lambda: {'pid': 123, 'paused': True,
                        'active_requests': 0, 'waiting_requests': 0})
    monkeypatch.setattr(restore.urllib.request, 'urlopen', lambda *_args, **_kwargs: pytest.fail('must not send'))
    restore.restore()


def test_restore_legacy_pause_refuses_busy_gate(monkeypatch):
    monkeypatch.setattr(restore, 'service_identity', lambda: (123, 'start'))
    monkeypatch.setattr(restore, 'read_status', lambda: {'pid': 123, 'paused': False,
                        'active_requests': 1, 'waiting_requests': 0})
    with pytest.raises(RuntimeError): restore.restore()


def test_startup_release_retains_legacy_and_other_admin_holds(tmp_path, monkeypatch):
    (tmp_path / 'gate-admin-key').write_text('test-admin-key')
    monkeypatch.setattr(startup, 'KEYS', tmp_path)
    monkeypatch.setattr(startup, 'read_status', lambda: None)
    gate = {'protocol': 3, 'durable': True, 'epoch': 'a' * 32, 'holds': ['admin-maintenance', 'legacy', 'startup'],
            'paused': True, 'active_requests': 0, 'waiting_requests': 0,
            'recovery_exclusive_hold': None, 'deployment_generation': None}
    after = {**gate, 'holds': ['admin-maintenance', 'legacy']}
    snapshots = iter([gate, after])
    monkeypatch.setattr(startup, 'get', lambda *_args: next(snapshots))
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self, *_args): return b'{"held": false}'
    monkeypatch.setattr(startup.urllib.request, 'urlopen', lambda *_args, **_kwargs: Response())
    assert startup.release() == ['admin-maintenance', 'legacy']


def test_startup_release_rejects_independent_client_hold(tmp_path, monkeypatch):
    (tmp_path / 'gate-admin-key').write_text('test-admin-key')
    monkeypatch.setattr(startup, 'KEYS', tmp_path)
    monkeypatch.setattr(startup, 'read_status', lambda: None)
    gate = {'protocol': 3, 'durable': True, 'epoch': 'a' * 32, 'holds': ['startup', 'client-' + 'a' * 32],
            'paused': True, 'active_requests': 0, 'waiting_requests': 0,
            'recovery_exclusive_hold': None, 'deployment_generation': None}
    monkeypatch.setattr(startup, 'get', lambda *_args: gate)
    monkeypatch.setattr(startup.urllib.request, 'urlopen', lambda *_args, **_kwargs: pytest.fail('must not release'))
    with pytest.raises(RuntimeError): startup.release()


def test_backup_rejects_incomplete_source_before_creating_destination(tmp_path):
    dest = tmp_path / 'backup'
    with pytest.raises(FileNotFoundError): backup.backup(tmp_path, dest)
    assert not dest.exists()


@pytest.mark.skipif('GNU tar' not in subprocess.run(['/usr/bin/tar', '--version'], capture_output=True, text=True).stdout,
                    reason='Linux GNU tar is required for ACL and SELinux backup')
def test_backup_restores_files_modes_and_cli_symlink(tmp_path):
    root = tmp_path / 'root'; root.mkdir()
    for name in backup.REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == 'usr/local/bin/ai-model':
            path.symlink_to('/opt/ai/scripts/ai-model')
        elif name.endswith(('/backend', '/config', '/dist')):
            path.mkdir(exist_ok=True)
            (path / 'fixture').write_text('test fixture')
        else:
            path.write_text('test fixture')
            path.chmod(0o600)
    destination = tmp_path / 'backup'
    labelled = root / 'opt/bc250-ai-studio/backend/fixture'
    if shutil.which('setfacl'):
        subprocess.run(['setfacl', '-m', 'u:65534:r--', str(labelled)], check=True)
    attributes = {name: os.getxattr(labelled, name) for name in os.listxattr(labelled)
                  if name in {'security.selinux', 'system.posix_acl_access'}}
    assert backup.backup(root, destination) == len(backup.REQUIRED)
    assert destination.stat().st_mode & 0o777 == 0o700
    assert (destination / 'prechange.tar').stat().st_mode & 0o777 == 0o600
    restored = tmp_path / 'restored'; restored.mkdir()
    subprocess.run(['/usr/bin/tar', '--acls', '--xattrs', '--selinux', '-xf',
                    str(destination / 'prechange.tar'), '-C', str(restored)], check=True)
    assert (restored / 'usr/local/bin/ai-model').readlink() == Path('/opt/ai/scripts/ai-model')
    assert (restored / 'etc/bc250-ai-studio.env').stat().st_mode & 0o777 == 0o600
    assert (restored / 'opt/bc250-ai-studio/backend/fixture').read_text() == 'test fixture'
    restored_file = restored / 'opt/bc250-ai-studio/backend/fixture'
    assert {name: os.getxattr(restored_file, name) for name in os.listxattr(restored_file)
            if name in {'security.selinux', 'system.posix_acl_access'}} == attributes
    with pytest.raises(FileExistsError): backup.backup(root, destination)
