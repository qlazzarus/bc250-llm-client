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
