"""Portable favorite-count snapshots, without catalog or personal library settings."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3

from favorites import HOSTS
from collector_identity import read_identity, validate_collector
from client_version import APP_VERSION

FORMAT = "local-tag-catalog-favorite-counts"
VERSION = 2
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_SQLITE_INTEGER = 2**63 - 1
MAX_TIMESTAMP = 253402300799
LEGACY_RECORD_FIELDS = {"gid", "favorite_count", "checked_at", "source"}
RECORD_FIELDS = LEGACY_RECORD_FIELDS | {"collector_id", "collector_name"}
ACCEPT_UPDATE = "i.favorite_count>f.favorite_count AND (i.origin_kind='other' OR i.checked_at>f.checked_at)"


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
    if not isinstance(payload, dict) or set(payload) != {"format", "version", "exported_at", "records"} or payload["format"] != FORMAT or type(payload["version"]) is not int or payload["version"] not in {1, VERSION}:
        raise ValueError("收藏数文件格式或版本不受支持，请使用「仅导出收藏数」生成的文件。")
    if not isinstance(payload["exported_at"], str) or not isinstance(payload["records"], list):
        raise ValueError("收藏数文件的导出时间或记录列表格式不正确。")
    seen = set()
    fields = LEGACY_RECORD_FIELDS if payload["version"] == 1 else RECORD_FIELDS
    for number, item in enumerate(payload["records"], 1):
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError(f"第 {number} 条记录字段与收藏数文件版本不符。")
        for field, minimum, maximum in (("gid", 1, MAX_SQLITE_INTEGER), ("favorite_count", 0, MAX_SQLITE_INTEGER), ("checked_at", 1, MAX_TIMESTAMP)):
            value = item[field]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"第 {number} 条记录的 {field} 不是有效整数。")
        if not isinstance(item["source"], str) or item["source"] not in HOSTS:
            raise ValueError(f"第 {number} 条记录的源站不受支持。")
        if item["gid"] in seen:
            raise ValueError(f"收藏数文件的作品 ID 重复（第 {number} 条），未导入。")
        seen.add(item["gid"])
        if payload["version"] == 1:
            item.update(collector_id="", collector_name="")
        try:
            validate_collector(item["collector_id"], item["collector_name"], allow_unknown=True)
        except ValueError as error:
            raise ValueError(f"第 {number} 条记录的采集者信息不正确：{error}") from error
    return payload


def export_snapshot(favorites_path):
    path = Path(favorites_path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        records = [dict(row) for row in db.execute("SELECT gid,favorite_count,checked_at,source,collector_id,collector_name FROM favorites ORDER BY gid")]
    payload = {"format": FORMAT, "version": VERSION, "exported_at": datetime.now(timezone.utc).isoformat(), "records": records}
    raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    # Every exported file must also satisfy the import contract, including size.
    parse_snapshot(raw)
    return raw, len(records)


def snapshot_share_info(raw):
    """Describe the exact exported snapshot without adding fields to its format."""
    payload = json.loads(raw)
    times = [item["checked_at"] for item in payload["records"]]
    return {
        "record_count": len(times),
        "oldest_checked_at": min(times, default=None),
        "newest_checked_at": max(times, default=None),
        "exported_at": payload["exported_at"],
        "client_version": APP_VERSION,
    }


def _incoming(db, records):
    own_id = read_identity(db)["collector_id"]
    db.execute("""CREATE TEMP TABLE incoming_favorites(
        gid INTEGER PRIMARY KEY,favorite_count INTEGER NOT NULL,checked_at INTEGER NOT NULL,source TEXT NOT NULL,
        collector_id TEXT NOT NULL,collector_name TEXT NOT NULL,origin_kind TEXT NOT NULL)
    """)
    def values():
        for item in records:
            collector_id = item.get("collector_id", "")
            origin = "unknown" if not collector_id else "own" if collector_id == own_id else "other"
            yield (item["gid"], item["favorite_count"], item["checked_at"], item["source"], collector_id, item.get("collector_name", ""), origin)
    db.executemany("INSERT INTO incoming_favorites VALUES(?,?,?,?,?,?,?)", values())


def _summary(db):
    row = db.execute(f"""
        SELECT COUNT(*),
               SUM(f.gid IS NULL),
               SUM(f.gid IS NOT NULL AND ({ACCEPT_UPDATE})),
               SUM(f.gid IS NOT NULL AND i.origin_kind!='other' AND i.checked_at<=f.checked_at),
               SUM(f.gid IS NOT NULL AND (i.origin_kind='other' OR i.checked_at>f.checked_at) AND i.favorite_count<=f.favorite_count),
               SUM(i.origin_kind='own'),SUM(i.origin_kind='other'),SUM(i.origin_kind='unknown')
        FROM incoming_favorites AS i LEFT JOIN main.favorites AS f ON f.gid=i.gid
    """).fetchone()
    total, added, updated, older, not_increased, own, other, unknown = (value or 0 for value in row)
    return {"total": total, "added": added, "updated": updated, "kept": older + not_increased, "kept_older_or_equal_time": older, "kept_count_not_increased": not_increased, "own_records": own, "other_records": other, "unknown_records": unknown}


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
            db.execute(f"""CREATE TEMP TABLE accepted_favorites AS
                SELECT i.* FROM incoming_favorites AS i LEFT JOIN main.favorites AS f ON f.gid=i.gid
                WHERE f.gid IS NULL OR ({ACCEPT_UPDATE})
            """)
            db.execute("""INSERT INTO favorites(gid,favorite_count,checked_at,source,collector_id,collector_name)
                SELECT gid,favorite_count,checked_at,source,collector_id,collector_name FROM accepted_favorites WHERE 1
                ON CONFLICT(gid) DO UPDATE SET favorite_count=excluded.favorite_count,checked_at=excluded.checked_at,
                    source=excluded.source,collector_id=excluded.collector_id,collector_name=excluded.collector_name
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
