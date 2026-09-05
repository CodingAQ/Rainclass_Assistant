"""跨普通/Debug 入口的单实例进程锁。"""

import os
from pathlib import Path
from typing import BinaryIO, Optional


class InstanceLock:
    """阻止多个 Rainclass Assistant 进程同时监控并重复调用 AI。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or Path(__file__).resolve().parents[1] / ".rainclass-assistant.lock"
        self._file: Optional[BinaryIO] = None

    def acquire(self) -> bool:
        if self._file is not None:
            return True

        handle = self.path.open("a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            handle.close()
            return False

        self._file = handle
        return True

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

