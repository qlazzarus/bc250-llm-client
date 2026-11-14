"""Exercise deployment failure handling in a temporary fake installation only."""
import os
from pathlib import Path
import shlex
import subprocess
import tarfile

import pytest


@pytest.mark.parametrize(('backend', 'frontend', 'failure'), [
    (True, True, 'restart'), (True, True, 'health'), (True, True, None),
    (True, False, None), (False, True, None), (True, False, 'health'),
])
def test_deployment_never_restarts_old_writer_on_failure(tmp_path, backend, frontend, failure):
    root = Path(__file__).resolve().parents[1]
    app = tmp_path / 'app'
    (app / 'backend').mkdir(parents=True)
    (app / 'backend' / 'version').write_text('old')
    (app / 'frontend' / 'dist').mkdir(parents=True)
    (app / 'frontend' / 'dist' / 'index.html').write_text('old-ui')
    (app / '.venv' / 'bin').mkdir(parents=True)
    python = app / '.venv' / 'bin' / 'python'
    python.write_text('#!/bin/sh\nprintf "pip\\n" >> "$DEPLOY_TEST_LOG"\nexit 0\n')
    python.chmod(0o755)
    candidate = tmp_path / 'candidate'
    (candidate / 'backend').mkdir(parents=True)
    (candidate / 'backend' / 'requirements.txt').write_text('')
    (candidate / 'backend' / 'version').write_text('new')
    (candidate / 'frontend' / 'dist').mkdir(parents=True)
    (candidate / 'frontend' / 'dist' / 'index.html').write_text('new-ui')
    with tarfile.open(app / '.deploy-release.tar.gz', 'w:gz') as archive:
        if backend: archive.add(candidate / 'backend', arcname='backend')
        if frontend: archive.add(candidate / 'frontend', arcname='frontend')
    bins = tmp_path / 'bin'
    bins.mkdir()
    log = tmp_path / 'sudo.log'
    sudo = bins / 'sudo'
    sudo.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DEPLOY_TEST_LOG"\n'
                    'case "$*" in *" -l "*) exit 0;; esac\n'
                    f'exit {1 if failure == "restart" else 0}\n')
    sudo.chmod(0o755)
    curl = bins / 'curl'
    curl.write_text(f'#!/bin/sh\nexit {1 if failure == "health" else 0}\n')
    curl.chmod(0o755)
    sleep = bins / 'sleep'
    sleep.write_text('#!/bin/sh\nexit 0\n')
    sleep.chmod(0o755)
    original = (root / 'deploy' / 'deploy-release.sh').read_text()
    assert 'app=/opt/bc250-llm-client' in original
    script = tmp_path / 'deploy.sh'
    script.write_text(original.replace('app=/opt/bc250-llm-client', 'app=' + shlex.quote(str(app)), 1))
    (app / '.deployed-components').write_text('old-state')
    backend_hash, frontend_hash = 'a' * 40, 'b' * 40
    result = subprocess.run(['bash', str(script), str(backend).lower(), str(frontend).lower(), backend_hash, frontend_hash], env={**os.environ, 'PATH': str(bins) + ':' + os.environ['PATH'],
                            'DEPLOY_TEST_LOG': str(log)}, capture_output=True, text=True, timeout=20)
    assert (app / 'backend' / 'version').read_text() == ('new' if backend else 'old')
    assert (app / 'frontend' / 'dist' / 'index.html').read_text() == ('new-ui' if frontend else 'old-ui')
    calls = log.read_text().splitlines() if log.exists() else []
    assert sum('restart bc250-llm-client.service' in line and '-l ' not in line for line in calls) == int(backend)
    assert ('pip' in calls) == backend
    stages = list(app.glob('.deploy.????????'))
    if failure:
        assert result.returncode != 0
        assert 'automatic rollback is disabled' in result.stderr
        assert len(stages) == 1
        assert not (app / '.deployed-components').exists()
        if backend: assert (stages[0] / 'old-backend' / 'version').read_text() == 'old'
        if frontend: assert (stages[0] / 'old-dist' / 'index.html').read_text() == 'old-ui'
        assert (app / '.deploy-release.tar.gz').exists()
    else:
        assert result.returncode == 0
        assert (app / '.deployed-components').read_text() == backend_hash + '\n' + frontend_hash + '\n'
        assert not stages
        assert not (app / '.deploy-release.tar.gz').exists()
