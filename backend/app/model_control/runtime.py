"""Production socket worker. Switching is disabled by default and requires verified coordination."""
import asyncio
import grp
import os
import pwd
import signal
import socket
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

from .adapters import AsyncGPULock, SwitchGate
from .bridge import MAX_MESSAGE, ModelControlBridge
from .client_trust import trusted_path
from .coordinator import ModelSwitch, SwitchJournal
from .files import FileLock
from .llm import SystemdLLM
from .operations import SwitchOperations
from .preflight import CoordinationPreflight, RecoveryPreflight
from .registry import ActiveModelWriter, ModelRegistry
from .systemd import SystemctlClient, SystemdModelService
from .worker import SwitchWorker

STATE = Path('/var/lib/bc250-model-control')
GPU_LOCK = Path('/run/bc250-gpu/gpu.lock')
WORKER_LOCK = Path('/run/bc250-gpu/model-worker.lock')
SOCKET = '/run/bc250-model-control/control.sock'
CLIENT_USER = 'yeppyshiba'


def credential(name, *, optional=False):
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory:
        raise RuntimeError('Systemd credentials required')
    value = (Path(directory) / name).read_bytes()
    if len(value) > 4096:
        raise RuntimeError('Invalid credential')
    value = value.decode('utf-8').rstrip('\r\n')
    if (not value and optional):
        return ''
    if len(value) < 24 or any(char.isspace() for char in value):
        raise RuntimeError('Invalid credential')
    return value


def validate_state_directory():
    fd = ModelRegistry(STATE)._open_base()
    os.close(fd)
    for name in ('operations.sqlite3', 'operations.sqlite3-wal', 'operations.sqlite3-shm'):
        try:
            info = (STATE / name).lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077):
            raise RuntimeError('Untrusted operation database')


def client_service_uid():
    """Authorize the configured Client process UID on the worker socket."""
    account = pwd.getpwnam(CLIENT_USER)
    if account.pw_uid == 0:
        raise RuntimeError('Unsafe Client service account')
    return account.pw_uid


def build_worker():
    if os.geteuid() != 0:
        raise RuntimeError('Model worker requires root')
    os.umask(0o077)
    validate_state_directory()
    journal = SwitchJournal(STATE / 'switch.json', secure_precreated=True)
    gate_key = credential('gate-key')
    llm_key = credential('llm-key', optional=True)
    studio_key = credential('studio-coordination-key')
    settings = SimpleNamespace(llm_service='llama-server.service', allowed_services=['llama-server.service'],
        systemctl=Path('/usr/bin/systemctl'), admission_url='http://127.0.0.1:8081/control',
        admission_key=gate_key, llm_url='http://127.0.0.1:8080', llm_api_key=llm_key, drain_timeout=120)
    registry = ModelRegistry()
    service = SystemdModelService(registry, ActiveModelWriter(registry), SystemctlClient(),
                                  health_timeout=240, settle_timeout=90, api_key=llm_key)
    gate = SwitchGate(settings.admission_url, gate_key, SystemdLLM(settings))
    lock = AsyncGPULock(GPU_LOCK)
    preflight = CoordinationPreflight(studio_key, gate_key, GPU_LOCK, STATE / 'switch.json', held_lock=lock.file_lock)
    switch = ModelSwitch(journal, lock, gate, service, preflight=preflight)
    recovery_preflight = RecoveryPreflight(studio_key, gate_key, GPU_LOCK, STATE / 'switch.json',
                                           held_lock=lock.file_lock, journal=journal)
    return SwitchWorker(SwitchOperations(STATE / 'operations.sqlite3'), switch,
                        FileLock(WORKER_LOCK, secure_precreated=True), verify_studio=recovery_preflight)


def inherited_socket():
    if os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
        raise RuntimeError('Exactly one systemd socket required')
    listener = socket.socket(fileno=3)
    if (listener.family != socket.AF_UNIX or listener.type != socket.SOCK_STREAM
            or listener.getsockname() != SOCKET or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)):
        listener.close()
        raise RuntimeError('Invalid systemd socket')
    try:
        trusted_path(Path(SOCKET).parent, directory=True)
        info = os.lstat(SOCKET)
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0
                or info.st_gid != grp.getgrnam(CLIENT_USER).gr_gid
                or stat.S_IMODE(info.st_mode) != 0o660):
            raise RuntimeError('Untrusted systemd socket permissions')
    except BaseException:
        listener.close()
        raise
    listener.setblocking(False)
    return listener


async def start_inherited_server(bridge, listener):
    # Python 3.13+ otherwise unlinks even an inherited systemd socket when
    # the server closes, preventing activation on the next service start.
    options = {'cleanup_socket': False} if sys.version_info >= (3, 13) else {}
    return await asyncio.start_unix_server(bridge.handle, sock=listener, limit=MAX_MESSAGE + 1, **options)


async def serve():
    listener = inherited_socket()
    enabled = os.environ.get('MODEL_CONTROL_SWITCH_ENABLED', 'false')
    if enabled not in {'true', 'false'}:
        raise RuntimeError('Invalid switch setting')
    allowed_uid = client_service_uid()
    bridge = ModelControlBridge(build_worker(), allowed_uid, switch_enabled=enabled == 'true')
    bridge.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        server = await start_inherited_server(bridge, listener)
        async with server:
            await stop.wait()
        await server.wait_closed()
    finally:
        await bridge.close()
        listener.close()


def main():
    try:
        asyncio.run(serve())
    except Exception:
        raise SystemExit('Model worker startup/runtime failure; inspect configuration and state') from None


if __name__ == '__main__':
    main()
