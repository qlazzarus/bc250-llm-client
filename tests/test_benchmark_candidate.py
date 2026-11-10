"""Benchmark callers must use gate epochs and stop on uncertain switches."""
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'deploy' / 'benchmark-candidate'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bench(tmp_path, monkeypatch):
    module = load('benchmark_candidate_test', 'benchmark.py')
    config = tmp_path / 'config'; (config / 'models').mkdir(parents=True)
    (config / 'active-model.conf').write_text('ACTIVE_MODEL=alpha\n')
    (config / 'models' / 'alpha.conf').write_text('MODEL_PATH=/models/alpha.gguf\n')
    monkeypatch.setattr(module, 'CONFIG', config)
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout='alpha\nbeta\n'))
    return module


class Reply:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, *args): return json.dumps(self.data).encode()


def test_benchmark_uses_single_gate_epoch_for_chat_and_token_count(bench, monkeypatch):
    observed = []
    def urlopen(request, timeout):
        if isinstance(request, str):
            assert request == 'http://127.0.0.1:8081/model-snapshot'
            return Reply({'ready': True, 'epoch': 'a' * 32, 'models': {'data': [{'id': '/models/alpha.gguf'}]}})
        body = json.loads(request.data)
        observed.append((request.full_url, request.get_header('X-model-epoch'), body))
        if request.full_url.endswith('/input_tokens'): return Reply({'input_tokens': 31})
        return Reply({'choices': [{'message': {'content': 'answer'}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr(bench.urllib.request, 'urlopen', urlopen)
    assert bench.start_snapshot('alpha')['epoch'] == 'a' * 32
    assert bench.count_tokens([{'role':'user','content':'hi'}]) == 31
    assert bench.chat([{'role':'user','content':'hi'}])['content'] == 'answer'
    assert all(epoch == 'a' * 32 and payload['model'] == '/models/alpha.gguf' for _, epoch, payload in observed)
    assert all(url.startswith('http://127.0.0.1:8081/') for url, _, _ in observed)
    bench.assert_snapshot()


@pytest.mark.parametrize('error', ['stale', 'timeout', 'invalid'])
def test_benchmark_stops_on_conflict_lost_response_or_invalid_reply(bench, monkeypatch, error):
    bench.SESSION = {'name':'alpha', 'model_id':'/models/alpha.gguf', 'epoch':'a' * 32}
    def urlopen(request, timeout):
        if error == 'stale': raise HTTPError(request.full_url, 409, 'conflict', {}, None)
        if error == 'timeout': raise URLError('timeout')
        return Reply({'choices': []})
    monkeypatch.setattr(bench.urllib.request, 'urlopen', urlopen)
    with pytest.raises(bench.BenchmarkInterrupted): bench.run_image_tests([{'id':'1','name':'x','system':'s','user':'u'}])


def test_snapshot_rejects_configured_model_mismatch_and_epoch_change(bench, monkeypatch):
    responses = iter([{'ready': True, 'epoch': 'a' * 32, 'models': {'data': [{'id': '/models/alpha.gguf'}]}},
                      {'ready': True, 'epoch': 'b' * 32, 'models': {'data': [{'id': '/models/alpha.gguf'}]}}])
    monkeypatch.setattr(bench.urllib.request, 'urlopen', lambda *a, **kw: Reply(next(responses)))
    bench.start_snapshot('alpha')
    with pytest.raises(bench.BenchmarkInterrupted): bench.assert_snapshot()
    with pytest.raises(bench.BenchmarkInterrupted): bench.start_snapshot('other')


def test_comparison_timeout_prints_key_and_operation_without_retry(monkeypatch, capsys):
    comparison = load('comparison_timeout_test', 'run-comparison.py')
    calls = []
    def command(args, timeout):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, timeout, output=b'operation_id=' + b'a' * 32 + b'\n')
    monkeypatch.setattr(comparison, 'command', command)
    with pytest.raises(comparison.SwitchOutcomeUnknown, match='operation=' + 'a' * 32): comparison.switch('alpha')
    assert len(calls) == 1 and calls[0][:3] == ['ai-model', 'switch', 'alpha']
    assert calls[0][3] == '--key' and len(calls[0][4]) == 32
    assert 'idempotency_key=' + calls[0][4] in capsys.readouterr().out


@pytest.mark.parametrize('failure', ['switch_unknown', 'epoch_changed', 'completion_unknown'])
def test_comparison_never_runs_later_switch_or_restore_after_failure(tmp_path, monkeypatch, failure):
    comparison = load('comparison_abort_test', 'run-comparison.py')
    (tmp_path / 'prompts.json').write_text('{}')
    (tmp_path / 'active-model.conf').write_text('ACTIVE_MODEL=original\n')
    monkeypatch.setattr(comparison, 'BASE', tmp_path)
    monkeypatch.setattr(comparison, 'CONF', tmp_path / 'active-model.conf')
    monkeypatch.setattr(comparison.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(comparison, 'service_active', lambda: True)
    monkeypatch.setattr(comparison.sys, 'argv', ['run-comparison', '--models', *comparison.MODELS[:2]])
    calls = []
    class Bench:
        def start_snapshot(self, name): calls.append(('snapshot', name))
        def assert_snapshot(self):
            calls.append(('check',))
            if failure == 'epoch_changed': raise RuntimeError('another model selected')
    monkeypatch.setattr(comparison, 'load_benchmark', lambda: Bench())
    def switch(name):
        calls.append(('switch', name))
        if failure == 'switch_unknown': raise comparison.SwitchOutcomeUnknown('unknown')
    monkeypatch.setattr(comparison, 'switch', switch)
    def suite(*args):
        if failure == 'completion_unknown': raise RuntimeError('inference outcome unknown')
        raise AssertionError('suite should not run')
    monkeypatch.setattr(comparison, 'run_suite', suite)
    assert comparison.main() == 1
    switches = [c for c in calls if c[0] == 'switch']
    assert len(switches) == (0 if failure == 'epoch_changed' else 1)
    report_path = next((tmp_path / 'comparisons').glob('*/comparison.json'))
    report = json.loads(report_path.read_text())
    assert 'not automatically restored' in report['restore_error']


def test_comparison_success_uses_shared_switch_to_restore_original(tmp_path, monkeypatch):
    comparison = load('comparison_success_test', 'run-comparison.py')
    (tmp_path / 'prompts.json').write_text('{}')
    monkeypatch.setattr(comparison, 'BASE', tmp_path)
    monkeypatch.setattr(comparison.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(comparison, 'service_active', lambda: True)
    monkeypatch.setattr(comparison.sys, 'argv', ['run-comparison', '--models', *comparison.MODELS[:2]])
    active = {'name': 'original'}
    calls = []
    class Bench:
        def start_snapshot(self, name): assert active['name'] == name
        def assert_snapshot(self): pass
    monkeypatch.setattr(comparison, 'active_model', lambda: active['name'])
    monkeypatch.setattr(comparison, 'load_benchmark', lambda: Bench())
    monkeypatch.setattr(comparison, 'run_suite', lambda *args: {'image': [], 'rp': [], 'long': [], 'summary': {}})
    monkeypatch.setattr(comparison, 'read_memory', lambda: {})
    def switch(name):
        calls.append(name)
        active['name'] = name
    monkeypatch.setattr(comparison, 'switch', switch)
    assert comparison.main() == 0
    assert calls == comparison.MODELS[:2] + ['original']


def test_comparison_never_starts_stopped_llama(tmp_path, monkeypatch):
    comparison = load('comparison_stopped_test', 'run-comparison.py')
    monkeypatch.setattr(comparison.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(comparison.sys, 'argv', ['run-comparison'])
    monkeypatch.setattr(comparison, 'service_active', lambda: False)
    monkeypatch.setattr(comparison, 'switch', lambda *_: pytest.fail('must not switch'))
    with pytest.raises(SystemExit, match='not running'): comparison.main()


@pytest.mark.parametrize('role', ['switch', 'studio'])
def test_benchmark_protocol3_gate_rejects_stale_then_accepts_new_snapshot(bench, tmp_path, monkeypatch, role):
    import httpx
    from fastapi.testclient import TestClient
    from gate_candidate.gate import create_app, provision
    seen = []
    class Body(httpx.AsyncByteStream):
        def __init__(self, data): self.data = data
        async def __aiter__(self): yield json.dumps(self.data).encode()
    def upstream(request):
        seen.append(request.url.path)
        if request.url.path == '/v1/models': data = {'data': [{'id': '/models/alpha.gguf'}]}
        elif request.url.path.endswith('/input_tokens'): data = {'input_tokens': 20}
        else: data = {'choices': [{'message': {'content': 'answer'}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, headers={'content-type': 'application/json'}, stream=Body(data))
    path = tmp_path / 'state'; provision(path)
    gate = create_app(path, 'http://upstream', admin_key='admin', studio_key='studio', switch_key='switch',
                      transport=httpx.MockTransport(upstream))
    with TestClient(gate) as client:
        assert client.delete('/control/holds/startup', headers={'Authorization':'Bearer admin'}).status_code == 200
        def urlopen(request, timeout):
            if isinstance(request, str): response = client.get('/model-snapshot')
            else: response = client.request(request.get_method(), request.selector, content=request.data, headers=dict(request.header_items()))
            if response.status_code >= 400:
                raise HTTPError(request if isinstance(request, str) else request.full_url, response.status_code, 'gate rejected', {}, None)
            return Reply(response.json())
        monkeypatch.setattr(bench.urllib.request, 'urlopen', urlopen)
        before = bench.start_snapshot('alpha')['epoch']
        assert bench.count_tokens([{'role':'user','content':'hi'}]) == 20
        hold = role + '-' + 'a' * 32
        assert client.put('/control/holds/' + hold, headers={'Authorization':'Bearer ' + role}).status_code == 200
        assert client.delete('/control/holds/' + hold, headers={'Authorization':'Bearer ' + role}).status_code == 200
        before_chat = seen.count('/v1/chat/completions')
        with pytest.raises(bench.BenchmarkInterrupted): bench.chat([{'role':'user','content':'old'}])
        assert seen.count('/v1/chat/completions') == before_chat
        assert bench.start_snapshot('alpha')['epoch'] != before
        assert bench.chat([{'role':'user','content':'new'}])['content'] == 'answer'
        assert seen.count('/v1/chat/completions') == before_chat + 1
