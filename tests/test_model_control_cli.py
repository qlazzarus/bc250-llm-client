import asyncio
from argparse import Namespace

import pytest

from backend.app.model_control.cli import run
from backend.app.model_control.client import WorkerUnavailable


@pytest.mark.parametrize(
    ('state', 'exit_code'),
    [('succeeded', 0), ('failed', 1), ('failed_rolled_back', 1), ('recovery_required', 2)],
)
def test_switch_cli_terminal_exit_codes(state, exit_code, capsys):
    class Client:
        async def request(self, action, **kwargs):
            assert action == 'submit'
            assert kwargs == {'model': 'new', 'idempotency_key': 'cli:key'}
            return {'id': 'a' * 32, 'state': state}

    assert asyncio.run(run(Namespace(command='switch', model='new', key='cli:key'), Client())) == exit_code
    output = capsys.readouterr().out
    assert 'idempotency_key=cli:key' in output and f'operation_id={"a" * 32}' in output


def test_switch_cli_returns_unknown_when_worker_is_unreachable(capsys):
    class Client:
        async def request(self, action, **kwargs):
            raise WorkerUnavailable()

    assert asyncio.run(run(Namespace(command='switch', model='new', key='cli:key'), Client())) == 2
    assert 'idempotency_key=cli:key' in capsys.readouterr().out

@pytest.mark.parametrize(('command', 'action'), [
    ('recover', 'recover'), ('resolve-verified', 'resolve_verified'),
])
def test_recovery_cli_sends_operation_and_reports_result(command, action, capsys):
    class Client:
        async def request(self, requested, **kwargs):
            assert requested == action
            assert kwargs == {'id': 'a' * 32}
            return {'id': kwargs['id'], 'state': 'failed_rolled_back', 'error_code': None}
    assert asyncio.run(run(Namespace(command=command, operation='a' * 32), Client())) == 0
    assert 'state=failed_rolled_back' in capsys.readouterr().out


def test_inspect_recovery_cli_prints_evidence(capsys):
    class Client:
        async def request(self, action, **kwargs):
            assert action == 'inspect_recovery' and kwargs == {}
            return {'journal': {'hold_id': 'switch-' + 'a' * 32}, 'operation': None}
    assert asyncio.run(run(Namespace(command='inspect-recovery'), Client())) == 0
    assert 'switch-' + 'a' * 32 in capsys.readouterr().out


def test_recovery_cli_reports_unknown_worker_result():
    class Client:
        async def request(self, action, **kwargs):
            raise WorkerUnavailable()
    assert asyncio.run(run(Namespace(command='recover', operation='a' * 32), Client())) == 2
