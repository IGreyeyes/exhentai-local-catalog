"""Keep packaged resources separate from the user's persistent library."""

from contextlib import contextmanager
from pathlib import Path
import os
import sys


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()


def library_root():
    override = os.environ.get("EH_CATALOG_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def helper_command(mode, *arguments):
    if getattr(sys, "frozen", False):
        return [sys.executable, mode, *map(str, arguments)]
    return [sys.executable, "-X", "utf8", str(resource_root() / "desktop.py"), mode, *map(str, arguments)]


@contextmanager
def library_lock(root):
    """Prevent two services from initializing or modifying one library at once."""
    data = Path(root) / "data"
    data.mkdir(parents=True, exist_ok=True)
    with (data / "service.lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("这个资料库已有服务在运行。请先关闭原窗口或停止浏览器版服务，再打开桌面版。") from error
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
