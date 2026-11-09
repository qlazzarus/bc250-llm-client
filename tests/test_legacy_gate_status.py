import io
import json

import pytest

from scripts import legacy_gate_status as status


@pytest.mark.parametrize('changed', [False, True])
def test_status_checks_identity_before_and_after_without_exposing_key(monkeypatch, capsys, changed):
    identities = iter([(123, '42'), (123, '43' if changed else '42')])
    monkeypatch.setattr(status, 'service_identity', lambda: next(identities))
    monkeypatch.setattr(status.Path, 'read_bytes', lambda _: b'GATE_API_KEY=secret\0')
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
    def reply(request, timeout):
        assert request.get_header('Authorization') == 'Bearer secret'
        return Response(b'{"paused":false,"active_requests":0,"waiting_requests":0}')
    monkeypatch.setattr(status.urllib.request, 'urlopen', reply)
    assert status.main() == (1 if changed else 0)
    output = capsys.readouterr()
    assert 'secret' not in output.out + output.err
    if not changed:
        assert json.loads(output.out)['pid'] == 123
