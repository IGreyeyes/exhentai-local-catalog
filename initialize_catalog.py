"""Validate the imported catalog and add indexes for local searches."""

from contextlib import closing
from datetime import datetime, timezone
import argparse
import json
import sqlite3
import time

from prepare_database import DEFAULT_DATABASE, inspect_database


def initialize(path=DEFAULT_DATABASE, progress=None):
    inspect_database(path)
    started = time.monotonic()
    with closing(sqlite3.connect(path)) as db:
        print("Checking SQLite integrity...", flush=True)
        if progress: progress("检查 SQLite 完整性")
        check = [row[0] for row in db.execute("PRAGMA quick_check")]
        if check != ["ok"]:
            raise ValueError(f"SQLite integrity check failed: {check}")
        db.execute("PRAGMA cache_size=-65536")
        for name, sql in (
            ("tag lookup", "CREATE INDEX IF NOT EXISTS local_tid_gid ON gid_tid(tid, gid)"),
            ("publication order", "CREATE INDEX IF NOT EXISTS local_posted_gid ON gallery(posted DESC, gid DESC)"),
        ):
            print(f"Preparing {name} index...", flush=True)
            if progress: progress("建立标签查询索引" if name=="tag lookup" else "建立发布时间索引")
            db.execute(sql)
            db.commit()
        print("Updating query planner statistics...", flush=True)
        if progress: progress("更新查询统计并清点作品记录")
        db.execute("ANALYZE")
        db.commit()
        row = db.execute("SELECT COUNT(*), MIN(posted), MAX(posted), SUM(removed != 0), SUM(replaced != 0), SUM(expunged != 0) FROM gallery").fetchone()
        report = {
            "gallery_count": row[0],
            "tag_count": db.execute("SELECT COUNT(*) FROM tag").fetchone()[0],
            "earliest_posted": row[1],
            "latest_posted": row[2],
            "removed_count": row[3],
            "replaced_count": row[4],
            "expunged_count": row[5],
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "size_bytes": path.stat().st_size,
            "has_favorites": False,
            "source_url": "https://github.com/URenko/e-hentai-db/releases/tag/nightly",
        }
    path.with_name("catalog_info.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    print(f"Ready in {time.monotonic() - started:.1f}s", flush=True)
    return report


if __name__ == "__main__":
    initialize()
