#!/usr/bin/python3
"""Check production Studio/gate process identity and isolated shared inodes."""
from pathlib import Path
import os
import sys
import tempfile

root = Path(sys.argv[1])
sys.dont_write_bytecode = True
sys.path.insert(0, str(root / 'install/lib'))
from backend.app.model_control.coordinator import SwitchJournal
from backend.app.model_control.files import FileLock, protected_xattrs
from backend.app.model_control.preflight import CoordinationPreflight

studio = CoordinationPreflight.service_identity('bc250-ai-studio.service', 'ai-studio')
gate = CoordinationPreflight.service_identity('bc250-llm-gate.service', 'ai-studio')
lock = FileLock(root / 'gpu.lock', secure_precreated=True)
lock.acquire()
try:
    check = CoordinationPreflight('test-studio-key', 'test-gate-key', root / 'gpu.lock',
                                  root / 'state/switch.json', held_lock=lock)
    check.same_studio_files(studio[0])
    journal = SwitchJournal(root / 'state/switch.json', secure_precreated=True)
    with journal.path.open('rb') as source:
        attrs = protected_xattrs(source.fileno())
    fd, temporary = tempfile.mkstemp(prefix='.m602-context-', dir=journal.path.parent)
    try:
        comparison = protected_xattrs(fd)
    finally:
        os.close(fd)
        os.unlink(temporary)
    print('existing_journal_selinux_context=' + attrs.get('security.selinux', b'<none>').decode(), flush=True)
    print('service_created_selinux_context=' + comparison.get('security.selinux', b'<none>').decode(), flush=True)
    print('existing_journal_has_extended_acl=' + str('system.posix_acl_access' in attrs), flush=True)
    old = journal.path.stat().st_ino
    journal.write({})
    with journal.path.open('rb') as source:
        after = protected_xattrs(source.fileno())
    assert old != journal.path.stat().st_ino and attrs == after
    check.same_studio_files(studio[0])
    assert studio == CoordinationPreflight.service_identity('bc250-ai-studio.service', 'ai-studio')
    assert gate == CoordinationPreflight.service_identity('bc250-llm-gate.service', 'ai-studio')
    print('studio_gate_identity_and_start_time=True')
    print('studio_namespace_shared_lock_and_journal_before_after_replace=True')
    print('atomic_replace_security_attributes_preserved=True')
finally:
    lock.release()
