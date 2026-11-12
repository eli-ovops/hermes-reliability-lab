from __future__ import annotations

import fcntl
from pathlib import Path
from types import TracebackType


class SingleInstanceBusy(RuntimeError):
    pass


class ExclusiveProcessLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = None

    def __enter__(self) -> "ExclusiveProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise SingleInstanceBusy("已有交易执行器持有进程锁") from exc
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
