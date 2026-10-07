"""Prepare a first local installation before launching the browser."""

from contextlib import closing, contextmanager
from pathlib import Path
import json
import os
import sqlite3

from initialize_catalog import initialize
from prepare_database import prepare
from update_translations import update


@contextmanager
def preparation_lock(data):
    data.mkdir(parents=True, exist_ok=True)
    with (data / "first-run.lock").open("a+b") as lock:
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
            raise ValueError("已有首次准备正在运行，请等待那个窗口完成，避免重复解压。") from error
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def catalog_prepared(database):
    info = database.with_name("catalog_info.json")
    if not database.is_file() or not info.is_file():
        return False
    try:
        metadata = json.loads(info.read_text(encoding="utf-8"))
        if not metadata.get("gallery_count") or not metadata.get("tag_count"):
            return False
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        return {"local_tid_gid", "local_posted_gid"} <= indexes
    except (OSError, ValueError, AttributeError, sqlite3.Error):
        return False


def prepare_library(root, progress=None):
    root = Path(root)
    progress = progress or (lambda message: print(message, flush=True))
    data = root / "data"
    # The service owns recovery; do not initialize an interrupted directory swap.
    if any((data / name).is_file() for name in ("catalog-swap.json", "restore-swap.json")):
        progress("发现待恢复的维护操作，启动服务后会先恢复资料。")
        return
    with preparation_lock(data):
        database = data / "catalog.sqlite3"
        if not database.is_file():
            archive = root / "e-hentai.db.zstd"
            if not archive.is_file():
                raise ValueError("请先下载 e-hentai.db.zstd 并放到程序文件夹，再重新打开应用。下载地址：https://github.com/URenko/e-hentai-db/releases/tag/nightly")
            if database.with_suffix(".sqlite3.partial").exists():
                raise ValueError("上次解压未完成。请确认没有其他准备任务，核对并另存 data/catalog.sqlite3.partial 后再重试；不要将未完成文件直接改名为正式数据库。")
            progress("[1/3] 首次准备：正在解压作品目录，请保持窗口打开…")
            prepare(archive, database, None)
        if not catalog_prepared(database):
            progress("[2/3] 正在检查作品目录并建立查询索引…")
            initialize(database)
        else:
            progress("作品目录已准备好，继续使用现有资料。")
        if not (data / "tag-translations.json").is_file():
            progress("[3/3] 正在准备中文词库…")
            try:
                update(destination=data / "tag-translations.json", progress=progress)
            except Exception:
                progress("中文词库暂未下载成功，将继续启动英文标签搜索；稍后可在「资料库维护 → 中文词库」中安装。")
        progress("资料已准备好，正在启动本地服务并打开浏览器…")
