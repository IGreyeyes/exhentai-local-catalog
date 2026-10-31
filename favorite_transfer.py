"""Portable favorite-count snapshots, without catalog or personal library settings."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import sqlite3

from favorites import HOSTS
from collector_identity import read_identity, validate_collector
from client_version import APP_VERSION

FORMAT = "local-tag-catalog-favorite-counts"
VERSION = 3
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_SQLITE_INTEGER = 2**63 - 1
MAX_TIMESTAMP = 253402300799
LEGACY_RECORD_FIELDS = {"gid", "favorite_count", "checked_at", "source"}
RECORD_FIELDS = LEGACY_RECORD_FIELDS | {"collector_id", "collector_name"}
FAILURE_FIELDS = {"gid", "token", "source", "failed_at", "attempts", "reason"}
FAILURE_REASONS = {
    "unavailable": "从分享文件导入：作品访问失败。",
    "network": "从分享文件导入：网络读取失败。",
    "access": "从分享文件导入：源站访问异常。",
    "parse": "从分享文件导入：收藏数解析失败。",
    "unknown": "从分享文件导入：抓取失败。",
}
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
    if not isinstance(payload, dict) or payload.get("format") != FORMAT or type(payload.get("version")) is not int or payload["version"] not in {1, 2, VERSION}:
        raise ValueError("收藏数文件格式或版本不受支持，请使用「导出收藏数和失败记录」生成的文件。")
    expected = {"format", "version", "exported_at", "records"} | ({"failures"} if payload["version"] == VERSION else set())
    if set(payload) != expected:
        raise ValueError("收藏数文件字段与版本不符，请重新导出。")
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
    failures = payload.setdefault("failures", [])
    if not isinstance(failures, list):
        raise ValueError("抓取失败记录必须是列表。")
    seen = set()
    for number, item in enumerate(failures, 1):
        if not isinstance(item, dict) or set(item) != FAILURE_FIELDS:
            raise ValueError(f"第 {number} 条失败记录字段不正确。")
        for field, minimum, maximum in (("gid", 1, MAX_SQLITE_INTEGER), ("failed_at", 1, MAX_TIMESTAMP), ("attempts", 0, MAX_SQLITE_INTEGER)):
            value = item[field]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"第 {number} 条失败记录的 {field} 不是有效整数。")
        if not isinstance(item["source"], str) or item["source"] not in HOSTS:
            raise ValueError(f"第 {number} 条失败记录的源站不受支持。")
        if not isinstance(item["token"], str) or (item["token"] and not re.fullmatch(r"[0-9a-fA-F]{10}", item["token"])):
            raise ValueError(f"第 {number} 条失败记录的作品访问标识不正确。")
        if not isinstance(item["reason"], str) or item["reason"] not in FAILURE_REASONS:
            raise ValueError(f"第 {number} 条失败记录的失败类型不正确。")
        if item["gid"] in seen:
            raise ValueError(f"失败记录的作品 ID 重复（第 {number} 条），未导入。")
        seen.add(item["gid"])
    return payload


def portable_failure_reason(error):
    """Export a fixed category, never raw exceptions containing local access settings."""
    text = str(error or "").lower()
    for reason, label in FAILURE_REASONS.items():
        if text == label.lower():
            return reason
    if any(word in text for word in ("cookie", "权限", "登录", "访问异常", "浏览器验证", "限流", "access denied", "forbidden", "http 403", "http 429")):
        return "access"
    if any(word in text for word in ("不可访问", "不可用", "不存在", "已下架", "unavailable", "http 404", "http 410")):
        return "unavailable"
    if any(word in text for word in ("网络", "连接", "超时", "connection", "timeout", "timed out", "network")):
        return "network"
    if any(word in text for word in ("解析", "未找到收藏", "收藏数字段", "parse")):
        return "parse"
    return "unknown"


def export_snapshot(favorites_path):
    path = Path(favorites_path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        records = [dict(row) for row in db.execute("SELECT gid,favorite_count,checked_at,source,collector_id,collector_name FROM favorites ORDER BY gid")]
        failures = []
        for row in db.execute("SELECT gid,token,source,failed_at,error,attempts FROM collection_failures ORDER BY gid"):
            item = {key: row[key] for key in ("gid", "token", "source", "failed_at", "attempts")}
            if not re.fullmatch(r"[0-9a-fA-F]{10}", item["token"] or ""):
                item["token"] = ""
            item["reason"] = portable_failure_reason(row["error"])
            failures.append(item)
    payload = {"format": FORMAT, "version": VERSION, "exported_at": datetime.now(timezone.utc).isoformat(), "records": records, "failures": failures}
    raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    # Every exported file must also satisfy the import contract, including size.
    parse_snapshot(raw)
    return raw, len(records)


def snapshot_share_info(raw):
    """Describe the exact exported snapshot without adding fields to its format."""
    payload = json.loads(raw)
    times = [item["checked_at"] for item in payload["records"]]
    failed_times = [item["failed_at"] for item in payload.get("failures", [])]
    return {
        "record_count": len(times),
        "oldest_checked_at": min(times, default=None),
        "newest_checked_at": max(times, default=None),
        "failure_count": len(failed_times),
        "oldest_failed_at": min(failed_times, default=None),
        "newest_failed_at": max(failed_times, default=None),
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


def _prepare_incoming(db, records, failures):
    _incoming(db, records)
    db.execute(f"""CREATE TEMP TABLE accepted_favorites AS
        SELECT i.* FROM incoming_favorites AS i LEFT JOIN main.favorites AS f ON f.gid=i.gid
        WHERE f.gid IS NULL OR ({ACCEPT_UPDATE})
    """)
    db.execute("CREATE UNIQUE INDEX accepted_favorite_gid ON accepted_favorites(gid)")
    db.execute("""CREATE TEMP TABLE incoming_failures(
        gid INTEGER PRIMARY KEY,token TEXT NOT NULL,source TEXT NOT NULL,failed_at INTEGER NOT NULL,
        attempts INTEGER NOT NULL,error TEXT NOT NULL)
    """)
    db.executemany("INSERT INTO incoming_failures VALUES(?,?,?,?,?,?)",
                   ((item["gid"],item["token"],item["source"],item["failed_at"],item["attempts"],FAILURE_REASONS[item["reason"]]) for item in failures))
    # Compare failures with the successful snapshot that the merge will retain.
    db.execute("""CREATE TEMP TABLE accepted_failures AS
        SELECT i.* FROM incoming_failures AS i
        LEFT JOIN main.favorites AS f ON f.gid=i.gid
        LEFT JOIN accepted_favorites AS a ON a.gid=i.gid
        LEFT JOIN main.collection_failures AS e ON e.gid=i.gid
        WHERE i.failed_at>COALESCE(a.checked_at,f.checked_at,0)
          AND (e.gid IS NULL OR i.failed_at>e.failed_at)
    """)
    db.execute("CREATE UNIQUE INDEX accepted_failure_gid ON accepted_failures(gid)")


def _failure_summary(db):
    row = db.execute("""SELECT COUNT(*),
        SUM(a.gid IS NOT NULL AND e.gid IS NULL),SUM(a.gid IS NOT NULL AND e.gid IS NOT NULL),
        SUM(i.failed_at<=COALESCE(s.checked_at,f.checked_at,0)),
        SUM(i.failed_at>COALESCE(s.checked_at,f.checked_at,0) AND e.gid IS NOT NULL AND e.failed_at>=i.failed_at)
        FROM incoming_failures AS i
        LEFT JOIN accepted_failures AS a ON a.gid=i.gid
        LEFT JOIN main.collection_failures AS e ON e.gid=i.gid
        LEFT JOIN main.favorites AS f ON f.gid=i.gid
        LEFT JOIN accepted_favorites AS s ON s.gid=i.gid
    """).fetchone()
    total, added, updated, newer_success, newer_failure = (value or 0 for value in row)
    return {"total":total,"added":added,"updated":updated,"kept":newer_success+newer_failure,
            "kept_newer_success":newer_success,"kept_newer_or_equal_failure":newer_failure}


def preview_snapshot(favorites_path, catalog_path, records, failures=()):
    path = Path(favorites_path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.execute("ATTACH DATABASE ? AS catalog", (Path(catalog_path).resolve().as_uri() + "?mode=ro",))
        db.execute("BEGIN")
        _prepare_incoming(db, records, failures)
        result = _summary(db)
        result["failures"] = _failure_summary(db)
        result["missing_catalog"] = db.execute("SELECT COUNT(*) FROM (SELECT gid FROM incoming_favorites UNION SELECT gid FROM incoming_failures) AS i LEFT JOIN catalog.gallery AS g ON g.gid=i.gid WHERE g.gid IS NULL").fetchone()[0]
    return result


def merge_snapshot(favorites_path, records, failures=()):
    with closing(sqlite3.connect(favorites_path, timeout=30)) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            _prepare_incoming(db, records, failures)
            result = _summary(db)
            result["failures"] = _failure_summary(db)
            db.execute("""INSERT INTO favorites(gid,favorite_count,checked_at,source,collector_id,collector_name)
                SELECT gid,favorite_count,checked_at,source,collector_id,collector_name FROM accepted_favorites WHERE 1
                ON CONFLICT(gid) DO UPDATE SET favorite_count=excluded.favorite_count,checked_at=excluded.checked_at,
                    source=excluded.source,collector_id=excluded.collector_id,collector_name=excluded.collector_name
            """)
            # Keep failures newer than the imported success, and all rejected records' failures.
            db.execute("""DELETE FROM collection_failures WHERE EXISTS(
                SELECT 1 FROM accepted_favorites AS i WHERE i.gid=collection_failures.gid AND i.checked_at>=collection_failures.failed_at
            )""")
            db.execute("""INSERT INTO collection_failures(gid,token,source,failed_at,error,attempts,job_id)
                SELECT gid,token,source,failed_at,error,attempts,0 FROM accepted_failures WHERE 1
                ON CONFLICT(gid) DO UPDATE SET token=excluded.token,source=excluded.source,
                    failed_at=excluded.failed_at,error=excluded.error,attempts=excluded.attempts
            """)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return result
