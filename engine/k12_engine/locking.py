"""Process-scoped OS locks for complete JSON read/modify/write operations."""
from contextlib import contextmanager
from pathlib import Path
import os

@contextmanager
def workspace_lock(cwd):
    root = Path(cwd) / '.k12'
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'metadata.lock').open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
