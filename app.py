"""Read-only local catalog search. Uses only the Python standard library."""

from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import argparse
import base64
import binascii
import json
import math
import re
import secrets
import sqlite3
import threading
import time

from prepare_database import DEFAULT_DATABASE, inspect_database
from translations import Translations
from favorites import Collector, FavoriteStore, JOIN_FAVORITES, JOIN_RATINGS, gallery_url
from maintenance import LibraryMaintenance, MaintenanceManager, recover_import
from covers import CoverCache, CoverUnavailable
from credentials import export_credentials, import_credentials
from favorite_transfer import MAX_FILE_BYTES, snapshot_share_info
from collector_identity import MAX_IDENTITY_BYTES, parse_identity
from runtime_paths import library_lock, library_root, resource_root

ROOT = library_root()
STATIC_ROOT = resource_root() / "static"
APP_ID = "local-tag-catalog-v1"
FAVORITE_RATING_SYSTEM = {
    "minimum_ratings": 20,
    "levels": [{"threshold": threshold, "label": label} for threshold, label in
               ((10, "杰作"), (7, "优秀"), (4, "良好"), (1, "一般"), (0, "冷门"))],
    "reference_url": "https://github.com/Mayriad/Mayriads-EH-Master-Script/wiki/Feature-descriptions#alternative-rating-system",
}
FAVORITE_RATING_RATIO = f"CASE WHEN rc.rating_count >= {FAVORITE_RATING_SYSTEM['minimum_ratings']} THEN CAST(f.favorite_count AS REAL) / rc.rating_count END"
SORTS = {
    "newest": "g.posted DESC, g.gid DESC",
    "oldest": "g.posted ASC, g.gid ASC",
    "rating": "CAST(g.rating AS REAL) DESC, g.gid DESC",
    "pages": "g.filecount DESC, g.gid DESC",
    "favorites": "(f.favorite_count IS NULL) ASC, f.favorite_count DESC, g.gid DESC",
    "favorites_per_rating": "favorite_rating_ratio DESC, g.gid DESC",
}
RECORD_SORTS = {
    "recorded_desc":"f.recorded_at DESC, f.gid DESC",
    "recorded_asc":"f.recorded_at ASC, f.gid ASC",
    "favorites_desc":"(f.favorite_count IS NULL) ASC,f.favorite_count DESC, f.gid DESC",
    "favorites_asc":"(f.favorite_count IS NULL) ASC,f.favorite_count ASC, f.gid DESC",
    "favorites_per_rating":"favorite_rating_ratio DESC, f.gid DESC",
    "rating_desc":"CAST(g.rating AS REAL) DESC, f.gid DESC",
    "state_updated_desc":"(rs.state_updated_at IS NULL) ASC,rs.state_updated_at DESC,f.gid DESC",
}
ALIASES = {"l": "language", "lang": "language", "a": "artist", "g": "group", "c": "character", "p": "parody", "o": "other", "f": "female", "m": "male", "x": "mixed"}


def describe_favorite_rating(item):
    count, ratings = item["favorite_count"], item["rating_count"]
    if count is None or ratings is None:
        return {"state": "unknown", "value": None, "label": "未知"}
    if ratings == 0:
        return {"state": "unrated", "value": None, "label": "未评分"}
    if ratings < FAVORITE_RATING_SYSTEM["minimum_ratings"]:
        return {"state": "insufficient", "value": None, "label": "样本不足"}
    # Integer division avoids floating-point rounding at decimal and level boundaries.
    tenths = count * 10 // ratings
    label = next(level["label"] for level in FAVORITE_RATING_SYSTEM["levels"] if tenths >= level["threshold"] * 10)
    return {"state": "rated", "value": tenths / 10, "label": label}


@dataclass(frozen=True)
class SearchSelection:
    tags: tuple
    excluded_tags: tuple
    explicit_excluded_tags: tuple
    blacklist_tags: tuple
    title: str
    include_inactive: bool
    category: str
    use_blacklist: bool


def normalized_tag(value):
    value = value.strip().lower().removesuffix("$").strip()
    if ":" in value:
        namespace, name = value.split(":", 1)
        value = ALIASES.get(namespace, namespace) + ":" + name.strip().strip('"').removesuffix("$")
    return value


def like_literal(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Catalog:
    def __init__(self, path, translations_path=None):
        self.path = Path(path).resolve()
        inspect_database(self.path)
        self.favorites = FavoriteStore(self.path.with_name("favorites.sqlite3"))
        self.slots = threading.BoundedSemaphore(2)
        info_path = self.path.with_name("catalog_info.json")
        if info_path.exists():
            self.info = json.loads(info_path.read_text(encoding="utf-8"))
        else:
            with self.connection() as db:
                self.info = {
                    "gallery_count": db.execute("SELECT COUNT(*) FROM gallery").fetchone()[0],
                    "tag_count": db.execute("SELECT COUNT(*) FROM tag").fetchone()[0],
                    "latest_posted": db.execute("SELECT MAX(posted) FROM gallery").fetchone()[0],
                }
        with self.connection() as db:
            self.available_tags = {row[0] for row in db.execute("SELECT name FROM tag")}
        self.translations = Translations(translations_path or self.path.with_name("tag-translations.json"), self.available_tags)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("ATTACH DATABASE ? AS collected", (self.favorites.path.as_uri()+"?mode=ro",))
        db.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 30
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        try:
            yield db
        finally:
            db.close()

    def status(self):
        return {**self.info, "app_id": APP_ID, "has_favorites": True, "ready": True, "translations": self.translations.status(), "favorite_rating_system": FAVORITE_RATING_SYSTEM}

    def cover_source(self, gid):
        if isinstance(gid, bool) or not isinstance(gid, int) or gid <= 0:
            raise ValueError("作品 ID 不正确。")
        with self.connection() as db:
            row = db.execute("SELECT thumb FROM gallery WHERE gid=?", (gid,)).fetchone()
        if row is None or not row[0]:
            raise ValueError("当前作品目录没有这张封面。")
        return row[0]

    def suggest(self, query):
        query = normalized_tag(query)[:200]
        if not query:
            return {"items": [], "labels": {}}
        with self.connection() as db:
            rows = db.execute(
                "SELECT name FROM tag WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 12",
                (like_literal(query) + "%",),
            ).fetchall()
            names = [row[0] for row in rows]
            if len(names) < 12 and ":" not in query:
                extra = db.execute(
                    "SELECT name FROM tag WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 24",
                    ("%:" + like_literal(query) + "%",),
                ).fetchall()
                names = list(dict.fromkeys(names + [row[0] for row in extra]))[:12]
        translated = self.translations.matches(query)
        names = list(dict.fromkeys(translated + names))[:12]
        return {"items": names, "labels": self.translations.labels(names)}

    def tag_labels(self, tags):
        if len(tags) > 24 or any(len(tag) > 200 for tag in tags):
            raise ValueError("标签数量或长度超出限制。")
        return {"labels": self.translations.labels(tags)}

    def build_filter(self, db, tags, title, include_inactive, category, excluded_tags=()):
        clauses, params, resolved = [], [], []
        for name in tags:
            row = db.execute("SELECT id, name FROM tag WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"数据库中没有这个标签：{name}。请从标签建议中选择完整名称。")
            count = db.execute("SELECT COUNT(*) FROM gid_tid WHERE tid = ?", (row[0],)).fetchone()[0]
            resolved.append((count, row[0]))
        resolved.sort()
        if resolved:
            clauses.append("g.gid IN (SELECT gid FROM gid_tid WHERE tid = ?)")
            params.append(resolved[0][1])
            for _, tid in resolved[1:]:
                clauses.append("EXISTS (SELECT 1 FROM gid_tid AS gt WHERE gt.gid = g.gid AND gt.tid = ?)")
                params.append(tid)
        if excluded_tags:
            excluded_ids = []
            for name in excluded_tags:
                row = db.execute("SELECT id FROM tag WHERE name = ?", (name,)).fetchone()
                if row is not None:
                    excluded_ids.append(row[0])
            if excluded_ids:
                placeholders = ",".join("?" for _ in excluded_ids)
                clauses.append(f"NOT EXISTS (SELECT 1 FROM gid_tid AS excluded WHERE excluded.gid=g.gid AND excluded.tid IN ({placeholders}))")
                params.extend(excluded_ids)
        if title:
            clauses.append("(g.title LIKE ? ESCAPE '\\' OR g.title_jpn LIKE ? ESCAPE '\\')")
            params.extend(["%" + like_literal(title) + "%"] * 2)
        if not include_inactive:
            clauses.extend(["g.removed = 0", "g.replaced = 0", "g.expunged = 0"])
        if category:
            clauses.append("g.category = ?")
            params.append(category)
        return " AND ".join(clauses) or "1", tuple(params)

    @lru_cache(maxsize=128)
    def count(self, where, params):
        with self.connection() as db:
            return db.execute("SELECT COUNT(*) FROM gallery AS g WHERE " + where, params).fetchone()[0]

    def selection(self, query, allow_empty=False, apply_blacklist=True):
        tags = tuple(sorted(set(normalized_tag(tag) for tag in query.get("tag", []) if tag.strip())))
        explicit_excluded = tuple(sorted(set(normalized_tag(tag) for tag in query.get("exclude_tag", []) if tag.strip())))
        title = query.get("title", [""])[0].strip()
        if len(tags) > 12 or len(explicit_excluded) > 200 or any(len(tag) > 200 for tag in (*tags, *explicit_excluded)) or len(title) > 200:
            raise ValueError("最多同时包含 12 个标签、排除 200 个标签，每项不超过 200 个字符。")
        if not allow_empty and not tags and not title:
            raise ValueError("请添加至少一个标签，或输入标题关键词。")
        tags = tuple(sorted(set(self.translations.resolve(tag) for tag in tags)))
        explicit_excluded = tuple(sorted(set(self.translations.resolve(tag) for tag in explicit_excluded)))
        missing = [tag for tag in (*tags, *explicit_excluded) if tag not in self.available_tags]
        if missing:
            raise ValueError(f"数据库中没有这个标签：{missing[0]}。请从标签建议中选择完整名称。")
        raw_use_blacklist = query.get("use_blacklist", ["1"])[0]
        if raw_use_blacklist not in {"0", "1"}:
            raise ValueError("黑名单开关值不正确。")
        use_blacklist = apply_blacklist and raw_use_blacklist == "1"
        blacklist = tuple(tag for tag in self.favorites.get_tag_blacklist() if tag in self.available_tags) if use_blacklist else ()
        excluded = tuple(sorted(set((*explicit_excluded, *blacklist))))
        conflict = sorted(set(tags).intersection(excluded))
        if conflict:
            raise ValueError(f"同一个标签不能同时包含和排除：{conflict[0]}。")
        include_inactive = query.get("include_inactive", ["0"])[0] == "1"
        category = query.get("category", [""])[0]
        if len(category) > 40:
            raise ValueError("无效的作品分类。")
        return SearchSelection(tags, excluded, explicit_excluded, blacklist, title, include_inactive, category, use_blacklist)

    def preferences(self):
        tags = tuple(tag for tag in self.favorites.get_tag_blacklist() if tag in self.available_tags)
        return {"tag_blacklist": list(tags), "tag_labels": self.translations.labels(tags),
                "records_view": self.favorites.get_record_view(), "search_view": self.favorites.get_search_view()}

    def set_preferences(self, payload):
        values = payload.get("tag_blacklist")
        if not isinstance(values, list) or len(values) > 200:
            raise ValueError("标签黑名单必须是列表，最多保存 200 项。")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in values):
            raise ValueError("黑名单标签格式不正确。")
        tags = tuple(sorted(set(self.translations.resolve(normalized_tag(value)) for value in values)))
        missing = [tag for tag in tags if tag not in self.available_tags]
        if missing:
            raise ValueError(f"数据库中没有这个标签：{missing[0]}。请从标签建议中选择完整名称。")
        self.favorites.set_tag_blacklist(tags)
        self.count.cache_clear()
        return {"tag_blacklist": list(tags), "tag_labels": self.translations.labels(tags)}

    def recorded(self, query):
        """List one record per collected work, retaining failures and earlier successful counts."""
        started=time.monotonic()
        selection=self.selection({"tag":query.get("tag",[])},allow_empty=True,apply_blacklist=False)
        tags=selection.tags
        text=query.get("q",[""])[0].strip()
        source=query.get("source",[""])[0]
        age=query.get("age",["all"])[0]
        reading_state=query.get("state",["all"])[0]
        opened=query.get("opened",["all"])[0]
        collection=query.get("collection",["all"])[0]
        job_text=query.get("job_id",[""])[0]
        if job_text and (not job_text.isascii() or not job_text.isdigit() or not 1<=int(job_text)<=9223372036854775807):raise ValueError("采集任务 ID 不正确。")
        job_id=int(job_text) if job_text else None
        sort=query.get("sort",["favorites_desc"])[0]
        if len(text)>200:raise ValueError("搜索关键词最多 200 个字符。")
        if source not in {"","exhentai.org","e-hentai.org"}:raise ValueError("请选择有效的采集来源。")
        if age not in {"all","7d","30d","90d","older30"}:raise ValueError("请选择有效的记录时间范围。")
        if reading_state not in {"all","none","planned","reading","watched","ignored"}:raise ValueError("请选择有效的阅读状态。")
        if opened not in {"all","yes","no"}:raise ValueError("请选择有效的点开状态。")
        if collection not in {"all","success","failed"}:raise ValueError("请选择有效的采集状态。")
        if sort not in RECORD_SORTS:raise ValueError("请选择有效的记录排序方式。")
        try:
            page=max(1,int(query.get("page",["1"])[0]))
            limit=min(100,max(1,int(query.get("limit",["40"])[0])))
        except ValueError as error:raise ValueError("页码和每页数量必须是整数。") from error
        if not self.slots.acquire(timeout=2):raise TimeoutError("正在处理其他查询，请稍后重试。")
        now=int(time.time())
        try:
            with self.connection() as db:
                db.execute("BEGIN")
                records_cte="""WITH recorded AS (
                    SELECT f.gid,f.favorite_count,f.checked_at,f.source AS last_success_source,f.collector_id,f.collector_name,
                           COALESCE(e.source,f.source) AS source,COALESCE(e.failed_at,f.checked_at) AS recorded_at,
                           CASE WHEN e.gid IS NULL THEN 'success' ELSE 'failed' END AS collection_status,
                           e.failed_at,e.error,e.attempts AS failure_attempts,e.job_id,e.token AS failure_token
                    FROM collected.favorites AS f LEFT JOIN collected.collection_failures AS e ON e.gid=f.gid
                    UNION ALL
                    SELECT e.gid,NULL,NULL,NULL,'','',e.source,e.failed_at,'failed',e.failed_at,e.error,e.attempts,e.job_id,e.token
                    FROM collected.collection_failures AS e WHERE NOT EXISTS(SELECT 1 FROM collected.favorites AS f WHERE f.gid=e.gid)
                ) """
                joined=" FROM recorded AS f LEFT JOIN collected.record_status AS rs ON rs.gid=f.gid LEFT JOIN gallery AS g ON g.gid=f.gid LEFT JOIN collected.rating_counts AS rc ON rc.gid=f.gid "
                summary_row=db.execute(records_cte+"""SELECT COUNT(*),SUM(f.checked_at>=?),MAX(f.checked_at),SUM(g.gid IS NULL),
                    SUM(COALESCE(rs.opened_count,0)>0),
                    SUM(COALESCE(rs.state,'none')='none'),SUM(rs.state='planned'),SUM(rs.state='reading'),SUM(rs.state='watched'),SUM(rs.state='ignored'),
                    SUM(f.collection_status='success'),SUM(f.collection_status='failed'),MAX(f.recorded_at)"""+joined,(now-7*86400,)).fetchone()
                where,values=self.build_filter(db,tags,"",True,"")
                clauses=[where];params=list(values)
                if text:
                    pattern="%"+like_literal(text)+"%"
                    if text.isascii() and text.isdigit() and len(text)<=19 and int(text)<=9223372036854775807:
                        clauses.append("(f.gid=? OR g.title LIKE ? ESCAPE '\\' OR g.title_jpn LIKE ? ESCAPE '\\')")
                        params.extend([int(text),pattern,pattern])
                    else:
                        clauses.append("(g.title LIKE ? ESCAPE '\\' OR g.title_jpn LIKE ? ESCAPE '\\')")
                        params.extend([pattern,pattern])
                if source:clauses.append("f.source=?");params.append(source)
                if collection!="all":clauses.append("f.collection_status=?");params.append(collection)
                if job_id is not None:clauses.append("f.job_id=?");params.append(job_id)
                if age!="all":
                    days=30 if age=="older30" else int(age[:-1])
                    clauses.append("f.recorded_at < ?" if age=="older30" else "f.recorded_at >= ?")
                    params.append(now-days*86400)
                if reading_state!="all":clauses.append("COALESCE(rs.state,'none')=?");params.append(reading_state)
                if opened!="all":clauses.append("COALESCE(rs.opened_count,0)>0" if opened=="yes" else "COALESCE(rs.opened_count,0)=0")
                condition=" WHERE "+" AND ".join(clauses)
                total=db.execute(records_cte+"SELECT COUNT(*)"+joined+condition,params).fetchone()[0]
                pages=max(1,math.ceil(total/limit));page=min(page,pages)
                sql=records_cte+"SELECT f.*,rc.rating_count,rc.checked_at AS rating_checked_at,"+FAVORITE_RATING_RATIO+" AS favorite_rating_ratio,COALESCE(rs.state,'none') AS reading_state,COALESCE(rs.opened_count,0) AS opened_count,rs.first_opened_at,rs.last_opened_at,rs.state_updated_at,g.gid AS catalog_gid,g.token,g.title,g.title_jpn,g.category,g.posted,g.filecount,g.rating,g.removed,g.replaced,g.expunged"+joined+condition+" ORDER BY "+RECORD_SORTS[sort]+" LIMIT ? OFFSET ?"
                items=[]
                for row in db.execute(sql,(*params,limit,(page-1)*limit)).fetchall():
                    item=dict(row)
                    item["favorite_rating"] = describe_favorite_rating(item)
                    item["metadata_available"]=item.pop("catalog_gid") is not None
                    item["cover_path"] = f"/api/cover/{item['gid']}" if item["metadata_available"] else None
                    failure_token=item.pop("failure_token")
                    token=str((failure_token if item["collection_status"]=="failed" else None) or item.get("token") or "")
                    item.pop("token",None)
                    item["has_saved_count"]=item["favorite_count"] is not None
                    item["tags"]=[entry[0] for entry in db.execute("SELECT t.name FROM gid_tid AS gt JOIN tag AS t ON t.id=gt.tid WHERE gt.gid=? ORDER BY t.name",(item["gid"],))] if item["metadata_available"] else []
                    item["source_url"]=gallery_url(item["source"],item["gid"],token)
                    items.append(item)
        finally:self.slots.release()
        return {
            "items":items,"total":total,"page":page,"pages":pages,"limit":limit,"tags":list(tags),"sort":sort,"collection":collection,"job_id":job_id,
            "collector_identity":self.favorites.collector_identity(),
            "summary":{"total":summary_row[0],"recent_7_days":summary_row[1] or 0,"last_checked_at":summary_row[2],"missing_metadata":summary_row[3] or 0,"opened":summary_row[4] or 0,
                       "states":{"none":summary_row[5] or 0,"planned":summary_row[6] or 0,"reading":summary_row[7] or 0,"watched":summary_row[8] or 0,"ignored":summary_row[9] or 0},
                       "successful":summary_row[10] or 0,"failed":summary_row[11] or 0,"last_recorded_at":summary_row[12]},
            "tag_labels":self.translations.labels([*tags,*(tag for item in items for tag in item["tags"])]),
            "elapsed_ms":round((time.monotonic()-started)*1000),"snapshot_at":now,
        }

    def collection_plan(self, query):
        selection = self.selection(query)
        with self.connection() as db:
            where, params = self.build_filter(
                db, selection.tags, selection.title, selection.include_inactive,
                selection.category, selection.excluded_tags,
            )
        canonical = {
            "tag":list(selection.tags), "exclude_tag":list(selection.excluded_tags),
            "use_blacklist":["0"], "title":[selection.title],
            "include_inactive":["1" if selection.include_inactive else "0"],
            "category":[selection.category],
        }
        return canonical, where, params

    def search(self, query):
        started = time.monotonic()
        selection = self.selection(query)
        sort = query.get("sort", ["newest"])[0]
        if sort not in SORTS:
            raise ValueError("请选择已支持的排序方式。")
        try:
            page = max(1, int(query.get("page", ["1"])[0]))
            limit = min(100, max(1, int(query.get("limit", ["40"])[0])))
        except ValueError as exc:
            raise ValueError("页码和每页数量必须是整数。") from exc
        if not self.slots.acquire(timeout=2):
            raise TimeoutError("正在处理其他查询，请稍后再试。")
        try:
            with self.connection() as db:
                where, params = self.build_filter(
                    db, selection.tags, selection.title, selection.include_inactive,
                    selection.category, selection.excluded_tags,
                )
                total = self.count(where, params)
                coverage = db.execute("SELECT COUNT(f.favorite_count),SUM(CASE WHEN f.checked_at>=? THEN 1 ELSE 0 END),MIN(f.checked_at),MAX(f.checked_at),COUNT(" + FAVORITE_RATING_RATIO + ") FROM gallery AS g" + JOIN_FAVORITES + JOIN_RATINGS + "WHERE " + where, (int(time.time())-7*86400,*params)).fetchone()
                pages = max(1, math.ceil(total / limit))
                page = min(page, pages)
                sql = "SELECT g.gid,g.token,g.title,g.title_jpn,g.category,g.posted,g.filecount,g.rating,g.removed,g.replaced,g.expunged,f.favorite_count,f.checked_at AS favorite_checked_at,f.source AS favorite_source,f.collector_id,f.collector_name,rc.rating_count,rc.checked_at AS rating_checked_at," + FAVORITE_RATING_RATIO + " AS favorite_rating_ratio FROM gallery AS g" + JOIN_FAVORITES + JOIN_RATINGS + "WHERE " + where
                sql += " ORDER BY " + SORTS[sort] + " LIMIT ? OFFSET ?"
                rows = db.execute(sql, params + (limit, (page - 1) * limit)).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    item["favorite_rating"] = describe_favorite_rating(item)
                    item["cover_path"] = f"/api/cover/{row['gid']}"
                    item["tags"] = [t[0] for t in db.execute("SELECT t.name FROM gid_tid AS gt JOIN tag AS t ON t.id=gt.tid WHERE gt.gid=? ORDER BY t.name", (row["gid"],))]
                    token = str(item.pop("token"))
                    item["gallery_path"] = f"/g/{row['gid']}/{token}/" if re.fullmatch(r"[0-9a-fA-F]{10}", token) else None
                    items.append(item)
        finally:
            self.slots.release()
        return {
            "items": items, "total": total, "page": page, "pages": pages,
            "limit": limit, "tags": list(selection.tags),
            "exclude_tags": list(selection.explicit_excluded_tags),
            "blacklist_tags": list(selection.blacklist_tags),
            "use_blacklist": selection.use_blacklist, "sort": sort,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "has_favorites": True, "include_inactive": selection.include_inactive,
            "collector_identity": self.favorites.collector_identity(),
            "favorite_coverage": {"known":coverage[0],"total":total,"fresh":coverage[1] or 0,"fresh_days":7,"oldest_checked_at":coverage[2],"newest_checked_at":coverage[3],"complete":total>0 and coverage[0]==total},
            "favorite_rating_ratio_coverage": {"known":coverage[4],"total":total},
            "tag_labels": self.translations.labels([*selection.tags, *selection.excluded_tags, *(tag for item in items for tag in item["tags"])]),
        }


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, catalog):
        self.catalog = catalog
        self.shutdown_token = secrets.token_urlsafe(32)
        self.action_token = secrets.token_urlsafe(32)
        self.catalog_gate = threading.RLock()
        super().__init__(address, Handler)
        self.collector = Collector(catalog)
        project_root = catalog.path.parent.parent if catalog.path.parent.name=="data" else catalog.path.parent
        self.cover_cache = CoverCache(project_root/"data"/"covers", project_root/"data"/"covers.sqlite3")
        self.maintenance = MaintenanceManager(LibraryMaintenance(catalog.path,project_root),self.exclusive_maintenance,self.reload_catalog,self.reload_translations,self.invalidate_collection_preview)
        self.collector.on_completed = self.maintenance.on_collection_completed

    @contextmanager
    def exclusive_maintenance(self):
        with self.catalog_gate:
            self.collector.pause()
            thread=self.collector.thread
            if thread and thread.is_alive():
                thread.join(timeout=35)
                if thread.is_alive():raise ValueError("采集请求尚未结束，请稍后重新应用更新。")
            yield

    def reload_catalog(self):
        catalog=Catalog(self.catalog.path)
        self.catalog.count.cache_clear()
        self.catalog=catalog
        self.collector.catalog=catalog
        self.collector.store=catalog.favorites
        self.collector.preview_data=None
        with catalog.favorites.connection() as db:
            stored=db.execute("SELECT value FROM collector_state WHERE key='cooldown_until'").fetchone()
            cooldown=db.execute("SELECT MAX(cooldown_until) FROM jobs").fetchone()[0] or 0
        self.collector.cooldown_until=max(self.collector.cooldown_until,cooldown,stored[0] if stored else 0)

    def reload_translations(self):
        with self.catalog_gate:
            translations = Translations(self.catalog.path.with_name("tag-translations.json"), self.catalog.available_tags)
            if not translations.status()["available"]:
                raise ValueError("新版词库无法载入，原词库已保留。")
            self.catalog.translations = translations
            with self.collector.lock:
                self.collector.preview_data = None

    def invalidate_collection_preview(self):
        with self.collector.lock:
            self.collector.preview_data = None

    def request_stop(self):
        self.maintenance.reserve_stop()
        threading.Thread(target=self.shutdown,daemon=True).start()

    def server_close(self):
        if hasattr(self,"maintenance"):
            self.maintenance.close()
        if hasattr(self,"collector"):
            self.collector.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalTagCatalog/1"

    def reply(self, data, content_type="application/json; charset=utf-8", status=200):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def reply_file(self, path, content_type, cache_control="private, max-age=86400"):
        try:
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            with path.open("rb") as source:
                while block := source.read(256 * 1024):
                    self.wfile.write(block)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def reply_attachment(self, body, filename, record_count=None, share_info=None):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if record_count is not None:
            self.send_header("X-Favorite-Record-Count", str(record_count))
        if share_info is not None:
            self.send_header("X-Favorite-Share-Info", json.dumps(share_info, separators=(",", ":")))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        if self.headers.get("Host", "").split(":", 1)[0] not in {"127.0.0.1", "localhost"}:
            self.reply({"error": "请通过本机地址访问。"}, status=403)
            return
        if len(self.path) > 12000:
            self.reply({"error": "查询内容过长。"}, status=414)
            return
        request = urlparse(self.path)
        try:
            query = parse_qs(request.query, max_num_fields=40)
            if request.path == "/api/maintenance":
                self.reply({**self.server.maintenance.status(), "cover_cache": self.server.cover_cache.stats()})
            elif match := re.fullmatch(r"/api/cover/(\d{1,18})", request.path):
                gid = int(match[1])
                try:
                    with self.server.catalog_gate:
                        source = self.server.catalog.cover_source(gid)
                    path, content_type = self.server.cover_cache.get(source)
                    self.reply_file(path, content_type)
                except (ValueError, CoverUnavailable):
                    self.reply_file(STATIC_ROOT / "cover-placeholder.svg", "image/svg+xml", "private, max-age=3600")
            elif request.path.startswith("/api/"):
                with self.server.catalog_gate:
                    if request.path == "/api/status":
                        self.reply({**self.server.catalog.status(),"action_token":self.server.action_token})
                    elif request.path == "/api/collector":
                        self.reply(self.server.collector.status())
                    elif request.path == "/api/preferences":
                        self.reply(self.server.catalog.preferences())
                    elif request.path == "/api/tags":
                        self.reply(self.server.catalog.suggest(query.get("q", [""])[0]))
                    elif request.path == "/api/tag-labels":
                        self.reply(self.server.catalog.tag_labels(query.get("tag", [])))
                    elif request.path == "/api/search":
                        self.reply(self.server.catalog.search(query))
                    elif request.path == "/api/records":
                        self.reply(self.server.catalog.recorded(query))
                    else:self.reply({"error":"接口不存在。"},status=404)
            else:
                files = {"/": ("index.html", "text/html"), "/records": ("records.html", "text/html"), "/records/": ("records.html", "text/html"), "/maintenance": ("maintenance.html", "text/html"), "/maintenance/": ("maintenance.html", "text/html"), "/maintenance.css": ("maintenance.css", "text/css"), "/service.js": ("service.js", "text/javascript"), "/records.js": ("records.js", "text/javascript"), "/records.css": ("records.css", "text/css"), "/app.js": ("app.js", "text/javascript"), "/collector.js": ("collector.js", "text/javascript"), "/maintenance.js": ("maintenance.js", "text/javascript"), "/style.css": ("style.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml"), "/cover-placeholder.svg": ("cover-placeholder.svg", "image/svg+xml")}
                files["/platform.js"] = ("platform.js", "text/javascript")
                files["/gallery-cards.js"] = ("gallery-cards.js", "text/javascript")
                files["/client-updates.css"] = ("client-updates.css", "text/css")
                if request.path not in files:
                    self.reply({"error": "页面不存在。"}, status=404)
                    return
                filename, mime = files[request.path]
                self.reply((STATIC_ROOT / filename).read_bytes(), mime + "; charset=utf-8")
        except ValueError as exc:
            self.reply({"error": str(exc)}, status=400)
        except (sqlite3.OperationalError, TimeoutError):
            self.reply({"error": "查询耗时较长或数据库正忙，请添加更具体的标签后重试。"}, status=503)
        except Exception:
            self.reply({"error": "查询遇到错误，请检查本地服务日志。"}, status=500)
            import traceback
            traceback.print_exc()

    def do_POST(self):
        if self.path == "/api/shutdown":
            token = self.headers.get("X-Local-Token", "")
            if not secrets.compare_digest(token, self.server.shutdown_token):
                self.reply({"error": "请求未授权。"}, status=403)
                return
            try:
                self.server.maintenance.reserve_stop()
            except ValueError as error:
                self.reply({"error":str(error)},status=409)
                return
            self.reply({"stopping": True})
            threading.Thread(target=self.server.shutdown,daemon=True).start()
            return
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host.split(":",1)[0] not in {"127.0.0.1","localhost"} or (origin and origin != "http://"+host) or not secrets.compare_digest(self.headers.get("X-Catalog-Token", ""),self.server.action_token):
            self.reply({"error": "页面会话已更新或请求来源不正确，请刷新本地页面后重试。"},status=403)
            return
        try:
            if self.path == "/api/maintenance/favorites-prepare":
                if self.headers.get_content_type() not in {"application/octet-stream", "application/json"}:
                    raise ValueError("请选择收藏数 JSON 文件。")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_FILE_BYTES:
                    raise ValueError("收藏数文件须为 1 字节～128 MiB。")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("收藏数文件上传不完整，请重新选择。")
                self.reply(self.server.maintenance.prepare_favorites_upload(raw))
                return
            if self.headers.get_content_type() != "application/json":
                raise ValueError("请求必须使用 JSON 格式。")
            length = int(self.headers.get("Content-Length","0"))
            if not 0 < length <= 32000:
                raise ValueError("请求内容大小不正确。")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload,dict):
                raise ValueError("请求格式不正确。")
            if self.path.startswith("/api/maintenance/"):
                manager=self.server.maintenance
                if self.path in {"/api/maintenance/collector-name", "/api/maintenance/collector-identity-create"}:
                    if set(payload) != {"collector_name"}:
                        raise ValueError("请只填写采集者昵称。")
                    result = manager.set_collector_name(payload["collector_name"], initialize=self.path.endswith("-create"))
                elif self.path == "/api/maintenance/collector-identity-export":
                    if payload:
                        raise ValueError("导出采集身份不接受额外选项。")
                    self.reply_attachment(manager.export_collector_identity(), "collector-identity.ehcollector.json")
                    return
                elif self.path in {"/api/maintenance/collector-identity-check", "/api/maintenance/collector-identity-import"}:
                    checking = self.path.endswith("-check")
                    if set(payload) != ({"file"} if checking else {"file", "expected_id"}):
                        raise ValueError("请选择并检查采集身份文件后再确认迁移。")
                    encoded = payload["file"]
                    if not isinstance(encoded, str) or len(encoded) > 4*((MAX_IDENTITY_BYTES+2)//3):
                        raise ValueError("采集身份文件超过 16 KiB 或格式不正确。")
                    try:
                        raw = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError):
                        raise ValueError("采集身份文件编码不正确。") from None
                    if checking:
                        result = manager.check_collector_identity(raw)
                    else:
                        result = manager.start("collector-identity-import", identity=parse_identity(raw), expected_collector_id=payload["expected_id"])
                elif self.path == "/api/maintenance/favorites-export":
                    if payload:
                        raise ValueError("导出收藏数不接受额外选项。")
                    body, count = manager.export_favorites()
                    from datetime import datetime, timezone
                    filename="favorites-"+datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")+".ehfavorites.json"
                    self.reply_attachment(body, filename, count, snapshot_share_info(body))
                    return
                elif self.path == "/api/maintenance/favorites-import":
                    if set(payload) != {"prepared_id"}:
                        raise ValueError("请使用已检查的收藏数文件确认合并。")
                    result=manager.start("favorites-import",identifier=payload["prepared_id"])
                elif self.path in {"/api/maintenance/translations-check", "/api/maintenance/translations-update"}:
                    if payload:
                        raise ValueError("词库操作不接受自定义下载地址或额外选项。")
                    result=manager.start(self.path.rsplit("/",1)[1])
                elif self.path.endswith("/backup"):
                    result=manager.start("backup",include_catalog=payload.get("include_catalog",False))
                elif self.path.endswith("/prepare"):
                    result=manager.start("prepare",expected_sha256=payload.get("sha256",""))
                elif self.path.endswith("/apply"):
                    result=manager.start("apply",identifier=payload.get("prepared_id",""),allow_older=payload.get("allow_older",False),create_backup=payload.get("create_backup",True))
                elif self.path.endswith("/settings"):
                    result=manager.configure(payload.get("backup_on_completion"),payload.get("backup_directory"))
                elif self.path.endswith("/prepare-restore"):
                    result=manager.start("prepare-restore",restore_path=payload.get("path",""),include_catalog=payload.get("include_catalog",False))
                elif self.path.endswith("/restore"):
                    result=manager.start("restore",identifier=payload.get("prepared_id",""))
                elif self.path.endswith("/pick-directory"):
                    result=manager.choose_directory(payload.get("purpose"),payload.get("initial",""))
                elif self.path.endswith("/stop"):
                    manager.reserve_stop()
                    with self.server.catalog_gate:
                        self.server.collector.close()
                    self.reply({"stopping":True})
                    threading.Thread(target=self.server.shutdown,daemon=True).start()
                    return
                else:
                    self.reply({"error":"接口不存在。"},status=404)
                    return
            elif self.path == "/api/search/view":
                with self.server.catalog_gate:
                    if self.server.maintenance.stopping:raise ValueError("服务正在停止，请重新启动后再操作。")
                    if set(payload) != {"view"}:
                        raise ValueError("请只提交搜索结果显示方式。")
                    result={"search_view":self.server.catalog.favorites.set_search_view(payload["view"])}
            elif self.path.startswith("/api/records/"):
                with self.server.catalog_gate:
                    if self.server.maintenance.stopping:raise ValueError("服务正在停止，请重新启动后再操作。")
                    if self.path.endswith("/opened"):
                        gid=payload.get("gid")
                        if isinstance(gid,bool) or not isinstance(gid,int):raise ValueError("作品 ID 不正确。")
                        result=self.server.catalog.favorites.mark_opened(gid)
                    elif self.path.endswith("/state"):
                        result=self.server.catalog.favorites.set_reading_state(payload.get("gids"),payload.get("state"))
                    elif self.path == "/api/records/view":
                        if set(payload) != {"view"}:
                            raise ValueError("请只提交记录页显示方式。")
                        result={"records_view":self.server.catalog.favorites.set_record_view(payload["view"])}
                    else:
                        self.reply({"error":"接口不存在。"},status=404)
                        return
            elif self.path == "/api/preferences":
                with self.server.catalog_gate:
                    if self.server.maintenance.stopping:raise ValueError("服务正在停止，请重新启动后再操作。")
                    result=self.server.catalog.set_preferences(payload)
            elif self.path == "/api/credentials/export":
                with self.server.catalog_gate:
                    if self.server.maintenance.stopping:raise ValueError("服务正在停止，请重新启动后再操作。")
                    with self.server.collector.lock:
                        body=export_credentials(self.server.collector.settings)
                self.reply_attachment(body,"exhentai-login.ehcred")
                return
            elif self.path == "/api/credentials/import":
                encoded=payload.get("file")
                options=payload.get("settings",{})
                if not isinstance(encoded,str) or len(encoded)>45000 or not isinstance(options,dict):
                    raise ValueError("登录文件或采集设置格式不正确。")
                try:
                    raw=base64.b64decode(encoded,validate=True)
                except (binascii.Error,ValueError):
                    raise ValueError("登录文件编码不正确。") from None
                credentials=import_credentials(raw)
                allowed={"interval","refresh_days","skip_existing","collection_mode","batch_size","rating_priority"}
                if any(key not in allowed for key in options):
                    raise ValueError("采集设置包含不支持的字段。")
                with self.server.catalog_gate:
                    if self.server.maintenance.stopping:raise ValueError("服务正在停止，请重新启动后再操作。")
                    result=self.server.collector.configure({**options,**credentials})
            elif self.path.startswith("/api/collector/"):
                with self.server.catalog_gate:
                    if self.server.maintenance.stopping:raise ValueError("服务正在停止，请重新启动后再操作。")
                    collector=self.server.collector
                    if self.path == "/api/collector/settings":
                        result=collector.configure(payload)
                    elif self.path in {"/api/collector/verify","/api/collector/start","/api/collector/preview"}:
                        query_text=payload.get("query","")
                        if not isinstance(query_text,str) or len(query_text)>12000:raise ValueError("搜索条件不正确。")
                        query=parse_qs(query_text,max_num_fields=40)
                        if self.path.endswith("verify"):
                            if not isinstance(payload.get("use_job",False),bool):raise ValueError("任务验证选项须为开关值。")
                            result=collector.verify(None if payload.get("use_job") else query)
                        elif self.path.endswith("preview"):result=collector.preview(query,payload.get("operation","start"))
                        else:result=collector.start(query,payload.get("preview_id"))
                    elif self.path.endswith("/pause"):result=collector.pause()
                    elif self.path.endswith("/resume"):result=collector.resume(preview_id=payload.get("preview_id"))
                    elif self.path.endswith("/retry"):result=collector.resume(retry_failed=True,preview_id=payload.get("preview_id"))
                    elif self.path.endswith("/next_batch"):result=collector.next_batch(preview_id=payload.get("preview_id"))
                    elif self.path.endswith("/next_round"):result=collector.next_round(preview_id=payload.get("preview_id"))
                    elif self.path.endswith("/cancel"):result=collector.pause(cancel=True)
                    else:
                        self.reply({"error":"接口不存在。"},status=404)
                        return
            else:
                self.reply({"error":"接口不存在。"},status=404)
                return
            self.reply(result)
        except (ValueError,TypeError,OverflowError) as error:
            self.reply({"error":str(error)},status=400)
        except sqlite3.Error:
            self.reply({"error":"数据库正在处理其他任务，请稍后再试。"},status=503)
        except Exception:
            self.reply({"error":"操作未完成，请检查本地服务状态。"},status=500)

    def log_message(self, format, *args):
        # Keep search terms and artwork metadata out of request logs.
        if args and len(args) > 1 and str(args[1]) not in {"200", "304"}:
            print(f"HTTP {args[1]}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()
    root = args.database.resolve().parent.parent if args.database.parent.name == "data" else args.database.resolve().parent
    with library_lock(root):
        serve(args, root)


def serve(args, root):
    recovery=recover_import(args.database)
    if recovery:print(recovery,flush=True)
    catalog = Catalog(args.database)
    server = Server(("127.0.0.1", args.port), catalog)
    runtime = root / "logs" / f"server-{server.server_port}.json"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.parent.mkdir(exist_ok=True)
    runtime.write_text(json.dumps({"port": server.server_port, "token": server.shutdown_token}), encoding="utf-8")
    print(f"Ready: http://127.0.0.1:{server.server_port} | {catalog.info['gallery_count']:,} catalog records", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if runtime.exists():
            saved = json.loads(runtime.read_text(encoding="utf-8"))
            if saved.get("token") == server.shutdown_token:
                runtime.unlink()


if __name__ == "__main__":
    main()
