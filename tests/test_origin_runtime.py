import hashlib
import os
from types import SimpleNamespace

import pytest

from backend.app.model_control import origin_runtime as runtime


@pytest.fixture
def runtime_fixture(tmp_path, monkeypatch):
    root = tmp_path / 'proc'
    proc = root / '123'
    (proc / 'ns').mkdir(parents=True)
    (root / 'self' / 'ns').mkdir(parents=True)
    (proc / 'ns' / 'mnt').write_text('namespace')
    os.link(proc / 'ns' / 'mnt', root / 'self' / 'ns' / 'mnt')
    (proc / 'root').symlink_to('/')
    (proc / 'stat').write_text('123 (python) ' + ' '.join(['S'] + ['0'] * 18 + ['456']))
    binary = tmp_path / 'python'
    binary.write_bytes(b'\x7fELF reviewed binary')
    info = binary.stat()
    line = f'1000-2000 r-xp 00000000 {os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x} {info.st_ino} {binary}\n'
    (proc / 'maps').write_text(line + '2000-3000 r-xp 00000000 00:00 0 [vdso]\n')
    monkeypatch.setattr(runtime, 'trusted_path', lambda *_a, **_kw: None)
    fstat = runtime.os.fstat
    def root_owned(fd):
        value = fstat(fd)
        return SimpleNamespace(st_mode=value.st_mode, st_uid=0, st_nlink=value.st_nlink,
                               st_dev=value.st_dev, st_ino=value.st_ino, st_size=value.st_size,
                               st_mtime_ns=value.st_mtime_ns, st_ctime_ns=value.st_ctime_ns)
    monkeypatch.setattr(runtime.os, 'fstat', root_owned)
    return root, proc, binary, line, {str(binary): hashlib.sha256(binary.read_bytes()).hexdigest()}


def test_reviewed_runtime_and_tampered_binary(runtime_fixture):
    root, _proc, binary, _line, policy = runtime_fixture
    assert runtime.inspect_runtime_mappings(123, policy, proc_root=root) == [str(binary)]
    binary.write_bytes(b'\x7fELF altered')
    with pytest.raises(RuntimeError, match='digest'):
        runtime.inspect_runtime_mappings(123, policy, proc_root=root)


def test_reviewed_file_mapping_cannot_be_writable_and_executable(runtime_fixture):
    root, proc, _binary, line, policy = runtime_fixture
    (proc / 'maps').write_text(line.replace('r-xp', 'rwxp'))
    with pytest.raises(RuntimeError, match='Writable executable'):
        runtime.inspect_runtime_mappings(123, policy, proc_root=root)


@pytest.mark.parametrize('fault', ['extra', 'deleted', 'anonymous_exec', 'missing', 'mount', 'inode'])
def test_runtime_rejects_unknown_maps_or_namespace(runtime_fixture, fault):
    root, proc, binary, line, policy = runtime_fixture
    if fault == 'extra': (proc / 'maps').write_text(line + '3000-4000 r--p 00000000 00:01 9 /unknown\n')
    elif fault == 'deleted': (proc / 'maps').write_text(line.rstrip() + ' (deleted)\n')
    elif fault == 'anonymous_exec': (proc / 'maps').write_text(line + '3000-4000 rwxp 00000000 00:00 0\n')
    elif fault == 'missing': (proc / 'maps').write_text('3000-4000 r-xp 00000000 00:00 0 [vdso]\n')
    elif fault == 'mount':
        (root / 'self' / 'ns' / 'mnt').unlink()
        (root / 'self' / 'ns' / 'mnt').write_text('other')
    else: (proc / 'maps').write_text(line.replace(f' {binary.stat().st_ino} ', ' 999999 '))
    with pytest.raises(RuntimeError):
        runtime.inspect_runtime_mappings(123, policy, proc_root=root)


def test_runtime_rejects_inaccessible_proc(runtime_fixture):
    root, proc, _binary, _line, policy = runtime_fixture
    (proc / 'maps').unlink()
    with pytest.raises(OSError):
        runtime.inspect_runtime_mappings(123, policy, proc_root=root)


def test_runtime_rejects_changing_maps(runtime_fixture, monkeypatch):
    from pathlib import Path
    root, proc, _binary, _line, policy = runtime_fixture
    read = Path.read_text
    calls = 0
    def changing(path, *args, **kwargs):
        nonlocal calls
        value = read(path, *args, **kwargs)
        if path == proc / 'maps':
            calls += 1
            if calls == 2:
                return value + '4000-5000 r--p 00000000 00:00 0\n'
        return value
    monkeypatch.setattr(Path, 'read_text', changing)
    with pytest.raises(RuntimeError, match='changed'):
        runtime.inspect_runtime_mappings(123, policy, proc_root=root)


def test_runtime_rejects_shared_writable_alias_of_reviewed_file(runtime_fixture):
    root, proc, _binary, line, policy = runtime_fixture
    # A separate writable shared alias can modify the file backing an RX map.
    alias = line.replace('1000-2000 r-xp', '3000-4000 rw-s')
    (proc / 'maps').write_text(line + alias)
    with pytest.raises(RuntimeError, match='Shared writable file mapping'):
        runtime.inspect_runtime_mappings(123, policy, proc_root=root)


def test_runtime_allows_private_writable_mapping_of_reviewed_file(runtime_fixture):
    root, proc, binary, line, policy = runtime_fixture
    alias = line.replace('1000-2000 r-xp', '3000-4000 rw-p')
    (proc / 'maps').write_text(line + alias)
    assert runtime.inspect_runtime_mappings(123, policy, proc_root=root) == [str(binary)]
