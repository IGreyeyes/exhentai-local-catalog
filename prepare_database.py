"""Stream the downloaded Zstandard catalog into a local SQLite file (Python 3.14)."""

from compression import zstd
from contextlib import closing
from pathlib import Path
from runtime_paths import library_root
import argparse
import hashlib
import sqlite3
import shutil
import time

ROOT = library_root()
DEFAULT_DATABASE = ROOT / "data" / "catalog.sqlite3"


def inspect_database(path: Path) -> dict:
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"gallery", "tag", "gid_tid"} - tables
        if missing:
            raise ValueError(f"Missing catalog tables: {', '.join(sorted(missing))}")
        result = {
            "tables": sorted(tables),
            "columns": {
                table: [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
                for table in ("gallery", "tag", "gid_tid")
            },
        }
        required = {
            "gallery": {"gid","token","title","title_jpn","category","posted","filecount","rating","removed","replaced","expunged"},
            "tag": {"id","name"}, "gid_tid": {"gid","tid"},
        }
        for table, columns in required.items():
            missing_columns = columns-set(result["columns"][table])
            if missing_columns:
                raise ValueError(f"Unsupported catalog schema: {table} lacks {', '.join(sorted(missing_columns))}")
        return result


def prepare(source: Path, destination: Path, expected_sha256: str | None, progress=None) -> None:
    if destination.exists():
        print(f"Existing database: {destination}", flush=True)
        print(inspect_database(destination), flush=True)
        return
    if not source.is_file():
        raise FileNotFoundError(f"Place e-hentai.db.zstd in {ROOT}")
    if expected_sha256:
        print("Checking archive SHA-256...", flush=True)
        with source.open("rb") as archive:
            digest = hashlib.file_digest(archive, "sha256").hexdigest()
        if digest.lower() != expected_sha256.lower():
            raise ValueError(f"Archive checksum mismatch: {digest}")
        print("Archive checksum verified.", flush=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    if temporary.exists():
        raise FileExistsError(f"Incomplete extraction already exists; inspect it first: {temporary}")
    total = 0
    started = reported = time.monotonic()
    print(f"Extracting to {temporary}...", flush=True)
    with zstd.open(source, "rb") as archive, temporary.open("xb") as output:
        while block := archive.read(8 * 1024 * 1024):
            if shutil.disk_usage(destination.parent).free < 512*1024**2:
                raise OSError("Not enough free space to continue extraction safely.")
            output.write(block)
            total += len(block)
            now = time.monotonic()
            if now - reported >= 5:
                print(f"Extracted {total / 1024**3:.2f} GiB; elapsed {now - started:.0f}s", flush=True)
                if progress:
                    progress(total)
                reported = now
    print(inspect_database(temporary), flush=True)
    temporary.rename(destination)
    print(f"Ready: {destination} ({total / 1024**3:.2f} GiB)", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "e-hentai.db.zstd")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--sha256")
    options = parser.parse_args()
    prepare(options.source, options.database, options.sha256)
