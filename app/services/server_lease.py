"""SQLite deployment uses one execution process; fail fast on a second owner."""

import os
from pathlib import Path


class ServerLease:
    def __init__(self, path):
        self.path = path + ".lock"
        self.file = None

    def __enter__(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("数据库已由另一个执行进程占用；SQLite 模式仅支持单 worker") from exc
        return self

    def __exit__(self, *args):
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()
