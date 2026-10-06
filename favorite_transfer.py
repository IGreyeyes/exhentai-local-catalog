"""Portable favorite-count snapshots, without catalog or personal library settings."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3

from favorites import HOSTS

FORMAT = "local-tag-catalog-favorite-counts"
VERSION = 1
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_SQLITE_INTEGER = 2**63 - 1
MAX_TIMESTAMP = 253402300799
RECORD_FIELDS = {"gid", "favorite_count", "checked_at", "source"}


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("收藏数文件包含重复字段，未导入。")
        result[key] = value
    return result


def parse_snapshot(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_FILE_BYTES:
        raise ValueError("请选择不超过 128 MiB 的收藏数 JSON 文件。")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError("收藏数文件不是有效的 UTF-8 JSON，或包含重复字段。") from error
    if not isinstance(payload, dict) or set(payload) != {"format", "version", "exported_at", "records"} or payload["format"] != FORMAT or type(payload["version"]) is not int or payload["version"] != VERSION:
        raise ValueError("收藏数文件格式或版本不受支持，请使用「仅导出收藏数」生成的文件。")
    if not isinstance(payload["exported_at"], str) or not isinstance(payload["records"], list):
        raise ValueError("收藏数文件的导出时间或记录列表格式不正确。")
    seen = set()
    for number, item in enumerate(payload["records"], 1):
        if not isinstance(item, dict) or set(item) != RECORD_FIELDS:
            raise ValueError(f"第 {number} 条记录字段不正确，仅接受作品 ID、收藏数、抓取时间和源站。")
        for field, minimum, maximum in (("gid", 1, MAX_SQLITE_INTEGER), ("favorite_count", 0, MAX_SQLITE_INTEGER), ("checked_at", 1, MAX_TIMESTAMP)):
            value = item[field]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"第 {number} 条记录的 {field} 不是有效整数。")
        if not isinstance(item["source"], str) or item["source"] not in HOSTS:
            raise ValueError(f"第 {number} 条记录的源站不受支持。")
        if item["gid"] in seen:
            raise ValueError(f"收藏数文件的作品 ID 重复（第 {number} 条），未导入。")
        seen.add(item["gid"])
    return payload


def export_snapshot(favorites_path):
    path = Path(favorites_path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        records = [dict(row) for row in db.execute("SELECT gid,favorite_count,checked_at,source FROM favorites ORDER BY gid")]
    payload = {"format": FORMAT, "version": VERSION, "exported_at": datetime.now(timezone.utc).isoformat(), "records": records}
    raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    # Every exported file must also satisfy the import contract, including size.
    parse_snapshot(raw)
    return raw, len(records)


def _incoming(db, records):
    db.execute("CREATE TEMP TABLE incoming_favorites(gid INTEGER PRIMARY KEY,favorite_count INTEGER NOT NULL,checked_at INTEGER NOT NULL,source TEXT NOT NULL)")
    db.executemany("INSERT INTO incoming_favorites VALUES(?,?,?,?)", ((item["gid"], item["favorite_count"], item["checked_at"], item["source"]) for item in records))


def _summary(db):
    row = db.execute("""
        SELECT COUNT(*),
               SUM(f.gid IS NULL),
               SUM(f.gid IS NOT NULL AND i.checked_at>f.checked_at AND i.favorite_count>f.favorite_count),
               SUM(f.gid IS NOT NULL AND i.checked_at<=f.checked_at),
               SUM(f.gid IS NOT NULL AND i.checked_at>f.checked_at AND i.favorite_count<=f.favorite_count)
        FROM incoming_favorites AS i LEFT JOIN main.favorites AS f ON f.gid=i.gid
    """).fetchone()
    total, added, updated, older, not_increased = (value or 0 for value in row)
    return {"total": total, "added": added, "updated": updated, "kept": older + not_increased, "kept_older_or_equal_time": older, "kept_count_not_increased": not_increased}


def preview_snapshot(favorites_path, catalog_path, records):
    path = Path(favorites_path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.execute("ATTACH DATABASE ? AS catalog", (Path(catalog_path).resolve().as_uri() + "?mode=ro",))
        db.execute("BEGIN")
        _incoming(db, records)
        result = _summary(db)
        result["missing_catalog"] = db.execute("SELECT COUNT(*) FROM incoming_favorites AS i LEFT JOIN catalog.gallery AS g ON g.gid=i.gid WHERE g.gid IS NULL").fetchone()[0]
    return result


def merge_snapshot(favorites_path, records):
    with closing(sqlite3.connect(favorites_path, timeout=30)) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            _incoming(db, records)
            result = _summary(db)
            db.execute("""CREATE TEMP TABLE accepted_favorites AS
                SELECT i.* FROM incoming_favorites AS i LEFT JOIN main.favorites AS f ON f.gid=i.gid
                WHERE f.gid IS NULL OR (i.checked_at>f.checked_at AND i.favorite_count>f.favorite_count)
            """)
            db.execute("""INSERT INTO favorites(gid,favorite_count,checked_at,source)
                SELECT gid,favorite_count,checked_at,source FROM accepted_favorites WHERE 1
                ON CONFLICT(gid) DO UPDATE SET favorite_count=excluded.favorite_count,checked_at=excluded.checked_at,source=excluded.source
                WHERE excluded.checked_at>favorites.checked_at AND excluded.favorite_count>favorites.favorite_count
            """)
            # Keep failures newer than the imported success, and all rejected records' failures.
            db.execute("""DELETE FROM collection_failures WHERE EXISTS(
                SELECT 1 FROM accepted_favorites AS i WHERE i.gid=collection_failures.gid AND i.checked_at>=collection_failures.failed_at
            )""")
            db.commit()
        except Exception:
            db.rollback()
            raise
    return result
