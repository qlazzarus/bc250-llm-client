"""Frozen Studio contract excerpts for AST-isolated tests, not a runnable service.

These fixtures do not verify the currently installed external Studio release.
"""
class GPUManager:

    def shared_lock_held_by_root(self):
        """Observe the kernel lock table without acquiring or releasing the lock.

        The requesting worker separately proves its own lock and file identity;
        a root-owned exclusive flock is necessary here, not proof of that worker.
        If procfs is unavailable or filtered, fail closed.
        """
        try:
            with self.file_lock._open_secure() as handle:
                info = os.fstat(handle.fileno())
                for line in Path('/proc/locks').read_text().splitlines():
                    parts = line.split()
                    if len(parts) != 8 or parts[1:4] != ['FLOCK', 'ADVISORY', 'WRITE']:
                        continue
                    device = parts[5].split(':')
                    if len(device) != 3 or int(device[0], 16) != os.major(info.st_dev) or int(device[1], 16) != os.minor(info.st_dev) or (int(device[2]) != info.st_ino) or (not parts[4].isdigit()):
                        continue
                    status = Path(f'/proc/{parts[4]}/status').read_text()
                    uid = next((row.split()[1:] for row in status.splitlines() if row.startswith('Uid:')), [])
                    if uid == ['0', '0', '0', '0']:
                        return True
        except (OSError, ValueError, RuntimeError):
            pass
        return False
