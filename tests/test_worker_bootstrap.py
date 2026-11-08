import importlib.util
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('worker_bootstrap', Path('deploy/model-control/worker-bootstrap.py'))
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


def test_install_tree_rejects_writeable_and_linked_entries(tmp_path, monkeypatch):
    # Parent trust is tested independently; tmp ancestors are intentionally user-owned.
    real_check = module.check
    def local_check(info, *, directory, uid=0):
        if info.st_ino in ancestor_inodes:
            return
        return real_check(info, directory=directory, uid=os.getuid())
    ancestor_inodes = {part.stat().st_ino for part in (tmp_path, *tmp_path.parents)}
    monkeypatch.setattr(module, 'check', local_check)
    root = tmp_path / 'install'; root.mkdir(mode=0o755)
    source = root / 'code.py'; source.write_text('pass'); source.chmod(0o644)
    module.validate_tree(root)
    source.chmod(0o666)
    with pytest.raises(RuntimeError): module.validate_tree(root)
    source.chmod(0o644)
    (root / 'alias').symlink_to(source)
    with pytest.raises(RuntimeError): module.validate_tree(root)
    (root / 'alias').unlink()
    (root / 'startup.pth').write_text('')
    with pytest.raises(RuntimeError): module.validate_tree(root)


def test_client_bootstrap_rejects_untrusted_source_before_import(tmp_path, monkeypatch):
    client_spec = importlib.util.spec_from_file_location('client_bootstrap', Path('deploy/model-control/client-bootstrap.py'))
    client = importlib.util.module_from_spec(client_spec); client_spec.loader.exec_module(client)
    ancestors = {part.stat().st_ino for part in (tmp_path, *tmp_path.parents)}
    real_check = client.check
    def local_check(info, *, directory):
        if info.st_ino in ancestors:
            return
        proxy = type('Info', (), {'st_mode': info.st_mode, 'st_uid': 0, 'st_nlink': info.st_nlink})()
        return real_check(proxy, directory=directory)
    monkeypatch.setattr(client, 'check', local_check)
    root = tmp_path / 'runtime'; root.mkdir(mode=0o755)
    (root / 'lib').mkdir(mode=0o755)
    code = root / 'lib' / 'app.py'; code.write_text('pass'); code.chmod(0o644)
    client.validate_tree(root)
    code.chmod(0o666)
    with pytest.raises(RuntimeError): client.validate_tree(root)
    code.chmod(0o644)
    (root / 'lib' / 'alias').symlink_to(code)
    with pytest.raises(RuntimeError): client.validate_tree(root)
    (root / 'lib' / 'alias').unlink()
    (root / 'lib' / 'injection.pth').write_text('')
    with pytest.raises(RuntimeError): client.validate_tree(root)
