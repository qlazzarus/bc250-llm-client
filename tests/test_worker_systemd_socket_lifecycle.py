"""Systemd owns the socket path, including across worker restarts."""
import asyncio
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.model_control.runtime import start_inherited_server


@pytest.mark.asyncio
async def test_inherited_socket_path_survives_worker_close():
    # Keep the path short enough for AF_UNIX on macOS and Linux.
    with tempfile.TemporaryDirectory(prefix='bc250-sock-', dir='/tmp') as directory:
        path = Path(directory) / 'socket'
        systemd_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        systemd_socket.bind(str(path))
        systemd_socket.listen()
        systemd_socket.setblocking(False)
        async def handler(reader, writer):
            writer.close()
            await writer.wait_closed()
        try:
            for _ in range(2):
                listener = systemd_socket.dup()
                server = await start_inherited_server(SimpleNamespace(handle=handler), listener)
                server.close()
                await server.wait_closed()
                assert path.exists()
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.close()
                await writer.wait_closed()
        finally:
            systemd_socket.close()
