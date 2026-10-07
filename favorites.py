"""Persistent favorite counts and a single, deliberately paced metadata collector."""

from contextlib import contextmanager
from collections import deque
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4
import json
import hashlib
import math
import re
import secrets
import sqlite3
import threading
import time

from collector_identity import read_identity, validate_collector

HOSTS = {"exhentai.org", "e-hentai.org"}
JOIN_FAVORITES = " LEFT JOIN collected.favorites AS f ON f.gid = g.gid "
READING_STATES = {"none", "planned", "reading", "watched", "ignored"}


def gallery_url(host,gid,token):
    if host not in HOSTS or not isinstance(gid,int) or isinstance(gid,bool) or gid<=0 or not re.fullmatch(r"[0-9a-fA-F]{10}",str(token or "")):
        return None
    return f"https://{host}/g/{gid}/{token}/"


class CollectionPaused(Exception):
    def __init__(self, message, cooldown=0):
        super().__init__(message)
        self.cooldown = cooldown


class GalleryUnavailable(Exception):
    pass


class RetryableFetch(Exception):
    pass


class FavoriteParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.values = []
        self.current = []

    def handle_starttag(self, tag, attributes):
        if self.depth:
            if tag not in {"br", "img", "input", "hr", "meta", "link", "wbr"}:
                self.depth += 1
        elif dict(attributes).get("id") == "favcount":
            self.depth = 1
            self.current = []

    def handle_endtag(self, tag):
        if tag in {"br", "img", "input", "hr", "meta", "link", "wbr"}:
            return
        if self.depth:
            self.depth -= 1
            if self.depth == 0:
                self.values.append("".join(self.current).strip())

    def handle_data(self, text):
        if self.depth:
            self.current.append(text)


def parse_favorite_count(html, expected_gid=None):
    lower = html.lower()
    if any(text in lower for text in ("ip address has been", "temporarily banned", "excessive pageloads", "too many requests")):
        raise CollectionPaused("源站提示请求受限，已暂停。请稍后在浏览器确认可正常访问，再继续。", 3600)
    if any(text in lower for text in ("cf-chl-", "just a moment...", "verify you are human", "checking your browser")):
        raise CollectionPaused("源站要求浏览器验证，已暂停。请在源站完成验证；程序不会自动处理验证页。")
    if "gallery not found" in lower:
        raise GalleryUnavailable("源站未找到这部作品，可能已删除或回退到旧版本。")
    if any(text in lower for text in ("gallery not available", "this gallery has been removed", "this gallery is unavailable", "this gallery is not available", "this gallery is pining for the fjords")):
        raise GalleryUnavailable("源站作品已不可访问。")
    if expected_gid is not None:
        identity = re.search(r"\b(?:var|let|const)\s+gid\s*=\s*(\d+)", html)
        if identity and int(identity[1]) != int(expected_gid):
            raise CollectionPaused("返回了其他作品的页面，已暂停以避免写错收藏数。")
    parser = FavoriteParser()
    parser.feed(html)
    if len(parser.values) != 1:
        raise CollectionPaused("未找到可靠的收藏数字段，可能是登录失效、提示页或页面格式变化。请检查源站访问。")
    value = parser.values[0].strip()
    if value.casefold() == "never":
        return 0
    if value.casefold() == "once":
        return 1
    match = re.fullmatch(r"(\d+|\d{1,3}(?:,\d{3})+)(?:\s+times?)?", value, re.I)
    if not match:
        raise CollectionPaused("收藏数字段格式无法识别，已暂停；没有把未知值记为零。")
    return int(match[1].replace(",", ""))


class SameGalleryRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        old, new = urlparse(request.full_url), urlparse(newurl)
        if new.scheme != "https" or new.hostname != old.hostname or new.path.rstrip("/") != old.path.rstrip("/"):
            raise CollectionPaused("源站跳转到了登录页或其他地址，已停止请求。请检查登录状态。")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch_favorites(settings, gid, token):
    if settings["host"] not in HOSTS or not re.fullmatch(r"[0-9a-fA-F]{10}", str(token)):
        raise GalleryUnavailable("作品链接信息无效。")
    url = f"https://{settings['host']}/g/{int(gid)}/{token}/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132.0.0.0 Safari/537.36", "Accept": "text/html", "Accept-Encoding": "identity"}
    if settings.get("cookies"):
        headers["Cookie"] = settings["cookies"]
    handlers = [SameGalleryRedirect()]
    if settings.get("proxy"):
        handlers.append(ProxyHandler({"https": settings["proxy"], "http": settings["proxy"]}))
    opener = build_opener(*handlers)
    try:
        with opener.open(Request(url, headers=headers), timeout=25) as response:
            if "text/html" not in response.headers.get("Content-Type", "").lower():
                raise CollectionPaused("源站没有返回作品网页，请检查账号权限及 Cookie。")
            content = response.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                raise CollectionPaused("作品页面大小异常，已暂停。")
        return parse_favorite_count(content.decode("utf-8", errors="replace"), gid)
    except HTTPError as error:
        code = error.code
        retry_after = error.headers.get("Retry-After", "")
        error.close()
        if code in {404, 410}:
            raise GalleryUnavailable(f"作品不可访问（HTTP {code}）。") from None
        if code == 429:
            if retry_after.isdigit():
                cooldown = max(60,int(retry_after))
            else:
                try:
                    cooldown = max(60,int(parsedate_to_datetime(retry_after).timestamp()-time.time()))
                except (TypeError,ValueError,OverflowError):
                    cooldown = 900
            raise CollectionPaused("源站限流（HTTP 429），已暂停；冷却结束后可手动继续。", cooldown) from None
        if code in {401, 403}:
            raise CollectionPaused(f"源站拒绝访问（HTTP {code}），请检查登录状态、权限或浏览器验证。") from None
        if code >= 500:
            raise RetryableFetch(f"源站暂时不可用（HTTP {code}）。") from None
        raise CollectionPaused(f"源站返回 HTTP {code}，已暂停。") from None
    except (URLError, TimeoutError, ConnectionError, OSError):
        raise RetryableFetch("网络连接失败或超时，请检查系统代理或网络。") from None


class FavoriteStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS favorites (
                    gid INTEGER PRIMARY KEY, favorite_count INTEGER NOT NULL CHECK(favorite_count >= 0),
                    checked_at INTEGER NOT NULL, source TEXT NOT NULL,
                    collector_id TEXT NOT NULL DEFAULT '', collector_name TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, query_json TEXT NOT NULL, host TEXT NOT NULL,
                    state TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0, cached INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, message TEXT NOT NULL DEFAULT '',
                    cooldown_until INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    job_id INTEGER NOT NULL, gid INTEGER NOT NULL, token TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', PRIMARY KEY(job_id,gid)
                );
                CREATE INDEX IF NOT EXISTS task_queue ON tasks(job_id,state,gid);
                CREATE TABLE IF NOT EXISTS collector_state (key TEXT PRIMARY KEY,value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS favorites_checked_gid ON favorites(checked_at DESC,gid DESC);
                CREATE INDEX IF NOT EXISTS favorites_count_gid ON favorites(favorite_count DESC,gid DESC);
                CREATE TABLE IF NOT EXISTS collection_failures (
                    gid INTEGER PRIMARY KEY,token TEXT NOT NULL,source TEXT NOT NULL,
                    failed_at INTEGER NOT NULL,error TEXT NOT NULL,attempts INTEGER NOT NULL,
                    job_id INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS failures_date_gid ON collection_failures(failed_at DESC,gid DESC);
                CREATE INDEX IF NOT EXISTS failures_job_gid ON collection_failures(job_id,gid);
                CREATE TABLE IF NOT EXISTS record_status (
                    gid INTEGER PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'none' CHECK(state IN ('none','planned','reading','watched','ignored')),
                    opened_count INTEGER NOT NULL DEFAULT 0 CHECK(opened_count >= 0),
                    first_opened_at INTEGER,
                    last_opened_at INTEGER,
                    state_updated_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS record_status_state_gid ON record_status(state,gid DESC);
                CREATE INDEX IF NOT EXISTS record_status_opened_gid ON record_status(last_opened_at DESC,gid DESC);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(favorites)")}
            for column in ("collector_id", "collector_name"):
                if column not in columns:
                    db.execute(f"ALTER TABLE favorites ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            # Legacy counts may include imported data, so their authors remain unknown.
            db.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('collector_id',?)", (str(uuid4()),))
            db.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('collector_name','')")
            db.execute("""INSERT OR IGNORE INTO app_settings(key,value)
                SELECT 'collector_identity_initialized',CASE WHEN value='' THEN '0' ELSE '1' END
                FROM app_settings WHERE key='collector_name'
            """)
            read_identity(db)
            if "selection_json" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN selection_json TEXT NOT NULL DEFAULT '{}'")
            if "priority" not in {row[1] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            if "current_round" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN current_round INTEGER NOT NULL DEFAULT 1")
            if "round_no" not in {row[1] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN round_no INTEGER NOT NULL DEFAULT 1")
            db.execute("CREATE INDEX IF NOT EXISTS task_priority_queue ON tasks(job_id,state,priority,gid DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS task_round_queue ON tasks(job_id,round_no,state,priority)")
            db.execute("CREATE INDEX IF NOT EXISTS task_attempted_gid ON tasks(gid,attempts)")
            if db.execute("SELECT 1 FROM app_settings WHERE key='failure_records_migrated'").fetchone() is None:
                # Old queues did not store per-item timestamps. Their task update time is the fallback.
                db.execute("""
                    INSERT OR IGNORE INTO collection_failures(gid,token,source,failed_at,error,attempts,job_id)
                    SELECT t.gid,t.token,j.host,j.updated_at,t.error,t.attempts,t.job_id
                    FROM tasks AS t JOIN jobs AS j ON j.id=t.job_id
                    WHERE t.state='failed'
                      AND NOT EXISTS(SELECT 1 FROM tasks AS newer WHERE newer.gid=t.gid AND newer.job_id>t.job_id AND newer.state IN ('done','failed','skipped'))
                      AND NOT EXISTS(SELECT 1 FROM favorites AS f WHERE f.gid=t.gid AND f.checked_at>j.updated_at)
                    ORDER BY t.job_id DESC
                """)
                db.execute("INSERT INTO app_settings(key,value) VALUES('failure_records_migrated','1')")

    def collector_identity(self):
        with self.connection() as db:
            return read_identity(db)

    def set_collector_name(self, name, initialize=False):
        if not isinstance(name, str):
            raise ValueError("采集者昵称必须是文本。")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            identity = read_identity(db)
            updated = validate_collector(identity["collector_id"], name.strip())
            initialized = db.execute("SELECT value FROM app_settings WHERE key='collector_identity_initialized'").fetchone()
            if initialize and initialized and initialized[0] == '1' and updated != identity:
                raise ValueError("采集身份已生成，请点击「修改昵称」修改；已有 ID 不会重新生成。")
            identity = updated
            self._set_identity(db, identity)
        return identity

    def set_collector_identity(self, identity, expected_id):
        identity = validate_collector(**identity)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if read_identity(db)["collector_id"] != expected_id:
                raise ValueError("当前采集身份已变化，请重新检查身份文件。")
            self._set_identity(db, identity)
        return identity

    @staticmethod
    def _set_identity(db, identity):
        db.executemany("UPDATE app_settings SET value=? WHERE key=?", ((value, key) for key, value in identity.items()))
        db.execute("INSERT INTO app_settings(key,value) VALUES('collector_identity_initialized','1') ON CONFLICT(key) DO UPDATE SET value='1'")
        db.execute("UPDATE favorites SET collector_name=? WHERE collector_id=?", (identity["collector_name"], identity["collector_id"]))

    def get_tag_blacklist(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM app_settings WHERE key='tag_blacklist'").fetchone()
        if row is None:
            return []
        try:
            values = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(values, list):
            return []
        return [value for value in values if isinstance(value, str) and 0 < len(value) <= 200]

    def set_tag_blacklist(self, tags):
        values = sorted(set(tags))
        with self.connection() as db:
            db.execute(
                "INSERT INTO app_settings(key,value) VALUES('tag_blacklist',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(values, ensure_ascii=False, separators=(",", ":")),),
            )
        return values

    def get_record_view(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM app_settings WHERE key='records_view'").fetchone()
        if row is None:
            return None
        if row[0] == "minimal-tags":
            return "minimal"
        return row[0] if row[0] in ("minimal", "compact", "extended", "thumbnails") else None

    def set_record_view(self, view):
        if view not in ("minimal", "compact", "extended", "thumbnails"):
            raise ValueError("请选择有效的记录页显示方式。")
        with self.connection() as db:
            db.execute("INSERT INTO app_settings(key,value) VALUES('records_view',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (view,))
        return view

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path.as_uri(), uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def save(self, gid, count, host):
        with self.connection() as db:
            self._save(db, gid, count, host)

    @staticmethod
    def _save(db, gid, count, host):
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("Invalid favorite count")
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        identity = read_identity(db)
        db.execute("""INSERT INTO favorites(gid,favorite_count,checked_at,source,collector_id,collector_name)
            VALUES (?,?,?,?,?,?) ON CONFLICT(gid) DO UPDATE SET
            favorite_count=excluded.favorite_count,checked_at=excluded.checked_at,source=excluded.source,
            collector_id=excluded.collector_id,collector_name=excluded.collector_name
        """, (gid,count,int(time.time()),host,identity["collector_id"],identity["collector_name"]))
        db.execute("DELETE FROM collection_failures WHERE gid=?",(gid,))

    @staticmethod
    def _save_failure(db,gid,token,host,error,attempts,job_id):
        db.execute("""INSERT INTO collection_failures VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(gid) DO UPDATE SET token=excluded.token,source=excluded.source,
            failed_at=excluded.failed_at,error=excluded.error,attempts=excluded.attempts,job_id=excluded.job_id
        """,(gid,token,host,int(time.time()),str(error),attempts,job_id))

    def mark_opened(self, gid):
        if isinstance(gid, bool) or not isinstance(gid, int) or gid <= 0:
            raise ValueError("作品 ID 不正确。")
        now = int(time.time())
        with self.connection() as db:
            if db.execute("SELECT 1 FROM favorites WHERE gid=? UNION ALL SELECT 1 FROM collection_failures WHERE gid=?", (gid,gid)).fetchone() is None:
                raise ValueError("这条作品还没有采集记录。")
            db.execute("""
                INSERT INTO record_status(gid,state,opened_count,first_opened_at,last_opened_at)
                VALUES (?,'none',1,?,?)
                ON CONFLICT(gid) DO UPDATE SET
                    opened_count=record_status.opened_count+1,
                    first_opened_at=COALESCE(record_status.first_opened_at,excluded.first_opened_at),
                    last_opened_at=excluded.last_opened_at
            """, (gid, now, now))
            row = db.execute("SELECT * FROM record_status WHERE gid=?", (gid,)).fetchone()
        return dict(row)

    def set_reading_state(self, gids, state):
        if state not in READING_STATES:
            raise ValueError("请选择有效的阅读状态。")
        if not isinstance(gids, list) or not 1 <= len(gids) <= 1000:
            raise ValueError("每次可标记 1～1000 条记录。")
        if any(isinstance(gid, bool) or not isinstance(gid, int) or gid <= 0 for gid in gids):
            raise ValueError("作品 ID 列表不正确。")
        unique = list(dict.fromkeys(gids))
        placeholders = ",".join("?" for _ in unique)
        now = int(time.time())
        with self.connection() as db:
            found = {row[0] for row in db.execute(f"SELECT gid FROM favorites WHERE gid IN ({placeholders}) UNION SELECT gid FROM collection_failures WHERE gid IN ({placeholders})", (*unique,*unique))}
            missing = [gid for gid in unique if gid not in found]
            if missing:
                raise ValueError(f"有 {len(missing)} 条作品还没有采集记录。")
            db.executemany("""
                INSERT INTO record_status(gid,state,state_updated_at) VALUES (?,?,?)
                ON CONFLICT(gid) DO UPDATE SET state=excluded.state,state_updated_at=excluded.state_updated_at
            """, ((gid, state, now) for gid in unique))
            rows = [dict(row) for row in db.execute(f"SELECT * FROM record_status WHERE gid IN ({placeholders}) ORDER BY gid", unique)]
        return {"updated": len(rows), "state": state, "items": rows}

    def remember_cooldown(self, until):
        with self.connection() as db:
            db.execute("INSERT INTO collector_state VALUES ('cooldown_until',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (int(until),))

    def latest(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                return None
            result = dict(row)
            counts = {entry[0]: entry[1] for entry in db.execute("SELECT state,COUNT(*) FROM tasks WHERE job_id=? GROUP BY state", (row["id"],))}
            result.update(pending=counts.get("pending",0), succeeded=counts.get("done",0), failed=counts.get("failed",0))
            result["cached"] += counts.get("skipped",0)
            result["query"] = json.loads(result.pop("query_json"))
            result["selection"] = json.loads(result.pop("selection_json","{}"))
            result["collection_mode"] = result["selection"].get("collection_mode","full")
            if result["collection_mode"] == "rounds":
                current = dict(db.execute("""
                    SELECT COUNT(*) AS total,
                           SUM(state='pending') AS pending,SUM(state='done') AS succeeded,
                           SUM(state='failed') AS failed,SUM(state='skipped') AS cached
                    FROM tasks WHERE job_id=? AND round_no=?
                """, (row["id"],row["current_round"])).fetchone())
                current = {key:value or 0 for key,value in current.items()}
                unfinished = db.execute("SELECT COUNT(DISTINCT round_no) FROM tasks WHERE job_id=? AND round_no<=? AND state='pending'",(row["id"],row["current_round"])).fetchone()[0]
                remaining = db.execute("SELECT COUNT(*) FROM tasks WHERE job_id=? AND round_no>? AND state='pending'",(row["id"],row["current_round"])).fetchone()[0]
                total_rounds = result["selection"]["round_count"]
                result["round_progress"] = {
                    "current":row["current_round"],"total":total_rounds,"active":current,
                    "completed":min(total_rounds,row["current_round"])-unfinished,"remaining":remaining,
                }
            result["recent_errors"] = []
            for entry in db.execute("SELECT gid,token,error,attempts,round_no FROM tasks WHERE job_id=? AND state='failed' ORDER BY gid DESC LIMIT 5", (row["id"],)):
                item=dict(entry)
                item["source_url"]=gallery_url(row["host"],item["gid"],item.pop("token"))
                result["recent_errors"].append(item)
            return result


class Collector:
    def __init__(self, catalog, fetcher=fetch_favorites, on_completed=None):
        self.catalog, self.store, self.fetcher = catalog, catalog.favorites, fetcher
        self.on_completed = on_completed
        self.lock = threading.RLock()
        self.thread = None
        self.interrupt = threading.Event()
        self.settings = None
        self.verified = False
        self.last_request = 0.0
        self.response_times = deque(maxlen=20)
        self.preview_data = None
        self.cooldown_until = 0
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET state='paused',message='服务重启后已暂停。请重新配置并验证登录信息，再继续。' WHERE state IN ('running','preparing')")
            row = db.execute("SELECT MAX(cooldown_until) FROM jobs").fetchone()
            self.cooldown_until = row[0] or 0
            stored = db.execute("SELECT value FROM collector_state WHERE key='cooldown_until'").fetchone()
            self.cooldown_until = max(self.cooldown_until,stored[0] if stored else 0)

    def status(self):
        with self.lock:
            job = self.store.latest()
            settings = self.settings or (job.get("selection",{}) if job else {})
            return {
                "configured": bool(self.settings), "verified": self.verified,
                "has_credentials": bool(settings.get("cookies")),
                "host": settings.get("host","exhentai.org"), "interval": (self.settings or {}).get("interval",4),
                "proxy": settings.get("proxy",""), "refresh_days": settings.get("refresh_days",7),
                "skip_existing": settings.get("skip_existing",False),
                "collection_mode": settings.get("collection_mode","rounds"),
                "batch_size": settings.get("batch_size",1000),
                "rating_priority": settings.get("rating_priority",False),
                "busy": bool(self.thread and self.thread.is_alive()),
                "cooldown_until": self.cooldown_until, "job": job,
            }

    def configure(self, values):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("请先暂停采集，等待当前请求结束后再修改设置。")
            host = values.get("host","exhentai.org")
            if host not in HOSTS:
                raise ValueError("请选择 ExHentai 或 E-Hentai。")
            cookies = []
            reuse = self.settings and self.settings["host"] == host and not any(str(values.get(name,"")).strip() for name in ("ipb_member_id","ipb_pass_hash","igneous"))
            for name in ("ipb_member_id","ipb_pass_hash","igneous"):
                value = str(values.get(name,"")).strip()
                if any(character in value for character in "\r\n;\x00") or len(value)>2048 or not value.isascii():
                    raise ValueError("Cookie 值格式不正确。请只填写对应的 Value。")
                if value:
                    cookies.append(f"{name}={value}")
                elif host == "exhentai.org" and not reuse:
                    raise ValueError(f"ExHentai 需要填写 {name}。")
            if values.get("ipb_member_id") and not str(values["ipb_member_id"]).strip().isdigit():
                raise ValueError("ipb_member_id 应为数字。")
            proxy = str(values.get("proxy","")).strip()
            if proxy:
                parsed = urlparse(proxy)
                if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"","/"}:
                    raise ValueError("代理请填写 HTTP 代理地址，例如 http://127.0.0.1:7890；系统网络已可访问时可留空。")
                try:
                    parsed.port
                except ValueError:
                    raise ValueError("代理端口不正确。") from None
            raw_interval = values.get("interval",4)
            if isinstance(raw_interval, bool):
                raise ValueError("请求间隔须为 1～60 秒。")
            interval = float(raw_interval)
            raw_days = values.get("refresh_days",7)
            if isinstance(raw_days,bool) or not re.fullmatch(r"[0-9]+",str(raw_days)):
                raise ValueError("缓存刷新天数须为整数。")
            days = int(raw_days)
            if not math.isfinite(interval) or not 1 <= interval <= 60 or not 1 <= days <= 3650:
                raise ValueError("请求间隔须为 1～60 秒，刷新周期须为 1～3650 天。")
            skip_existing = values.get("skip_existing",False)
            if not isinstance(skip_existing,bool):
                raise ValueError("跳过已采集选项须为开关值。")
            mode=values.get("collection_mode",self.settings.get("collection_mode","full") if self.settings else "full")
            if mode not in {"rounds","batch","full"}:raise ValueError("请选择分轮、分批或全量采集。")
            raw_batch=values.get("batch_size",100)
            if isinstance(raw_batch,bool) or not re.fullmatch(r"[0-9]+",str(raw_batch)) or not 1 <= int(raw_batch) <= 100000:
                raise ValueError("每轮 / 每批数量须为 1～100000 的整数。")
            batch_size=int(raw_batch)
            rating_priority=values.get("rating_priority",False)
            if not isinstance(rating_priority,bool):
                raise ValueError("评分优先选项须为开关值。")
            cookie_header = self.settings["cookies"] if reuse else "; ".join(cookies)
            same_access = self.settings and all(self.settings.get(key)==value for key,value in (("host",host),("cookies",cookie_header),("proxy",proxy)))
            self.settings = {"host":host,"cookies":cookie_header,"proxy":proxy,"interval":interval,"refresh_days":days,"skip_existing":skip_existing,"collection_mode":mode,"batch_size":batch_size,"rating_priority":rating_priority}
            self.verified = bool(self.verified and same_access)
            self.preview_data = None
            if not same_access:
                self.response_times.clear()
        return self.status()

    def _fetch(self, settings, gid, token):
        started = time.monotonic()
        count = self.fetcher(settings,gid,token)
        self.response_times.append(max(0,time.monotonic()-started))
        return count

    def _eligibility(self, cutoff):
        if self.settings.get("skip_existing"):
            return "f.gid IS NULL", ()
        return "(f.checked_at IS NULL OR f.checked_at < ?)", (cutoff,)

    @staticmethod
    def _candidate_sql(where,eligible,rating_priority=False):
        ordering = "attempted ASC,rating DESC,gid DESC" if rating_priority else "known ASC,attempted ASC,year_rank ASC,(year=0) ASC,year DESC"
        return """
            WITH candidates AS (
                SELECT g.gid,g.token,CAST(g.rating AS REAL) AS rating,
                       CASE WHEN g.posted>0 THEN COALESCE(CAST(strftime('%Y',g.posted,'unixepoch','+8 hours') AS INTEGER),0) ELSE 0 END AS year,
                       CASE WHEN f.gid IS NULL THEN 0 ELSE 1 END AS known,
                       EXISTS(SELECT 1 FROM main.tasks AS previous WHERE previous.gid=g.gid AND previous.attempts>0) AS attempted
                FROM catalog.gallery AS g LEFT JOIN main.favorites AS f ON f.gid=g.gid
                WHERE ("""+where+") AND "+eligible+"""
            ), ranked AS (
                SELECT *,ROW_NUMBER() OVER(PARTITION BY known,attempted,year ORDER BY rating DESC,gid DESC) AS year_rank
                FROM candidates
            )
            SELECT gid,token,year,known,ROW_NUMBER() OVER (ORDER BY """+ordering+""" )-1 AS priority
            FROM ranked ORDER BY priority
        """

    @classmethod
    def _batch_candidates(cls,db,where,params,eligible,extra,limit,rating_priority=False):
        # SQL ranks the whole eligible catalog; only the previewed round enters memory.
        sql=cls._candidate_sql(where,eligible,rating_priority)+" LIMIT ?"
        return [dict(row) for row in db.execute(sql,(*params,*extra,limit))]

    @staticmethod
    def _candidate_signature(items):
        if items is None:return None
        value=json.dumps([(item["gid"],item["token"],item["year"],item["known"]) for item in items],separators=(",",":"))
        return hashlib.sha256(value.encode()).hexdigest()

    def _require_ready(self, verification=False):
        if not self.settings:
            raise ValueError("请先配置源站与登录信息。")
        if not verification and not self.verified:
            raise ValueError("请先点击“验证并读取一条”，确认可以读取收藏数。")
        if time.time() < self.cooldown_until:
            raise ValueError("源站冷却时间尚未结束，请稍后再试。")

    def _verification_candidate(self, query, cutoff, excluded):
        eligible, eligibility_params = self._eligibility(cutoff)
        placeholders=",".join("?" for _ in excluded)
        if query is None:
            job=self.store.latest()
            if not job or job["state"] not in {"paused","awaiting_next","completed_with_errors"}:
                raise ValueError("当前没有需要继续验证的已保存任务。")
            if job["host"]!=self.settings["host"]:
                raise ValueError("请切换回创建任务时使用的源站。")
            target=job["current_round"]+(job["state"]=="awaiting_next")
            skip=" AND t.gid NOT IN ("+placeholders+")" if excluded else ""
            with self.store.connection() as db:
                row=db.execute("SELECT t.gid,t.token FROM tasks AS t LEFT JOIN favorites AS f ON f.gid=t.gid WHERE t.job_id=? AND t.round_no<=? AND t.state IN ('pending','failed') AND "+eligible+skip+" ORDER BY (t.state='failed'),t.priority,t.gid DESC LIMIT 1",(job["id"],target,*eligibility_params,*excluded)).fetchone()
            return row,job["id"]
        _,where,params=self.catalog.collection_plan(query)
        if excluded:
            where="("+where+") AND g.gid NOT IN ("+placeholders+")"
            params=(*params,*excluded)
        if self.settings.get("collection_mode") in {"rounds","batch"}:
            with self.store.connection() as db:
                db.execute("ATTACH DATABASE ? AS catalog",(self.catalog.path.as_uri()+"?mode=ro",))
                candidates=self._batch_candidates(db,where,params,eligible,eligibility_params,1,self.settings.get("rating_priority",False))
                row=candidates[0] if candidates else None
        else:
            with self.catalog.connection() as db:
                order="CAST(g.rating AS REAL) DESC,g.gid DESC" if self.settings.get("rating_priority") else "g.gid DESC"
                row=db.execute("SELECT g.gid,g.token FROM gallery AS g"+JOIN_FAVORITES+"WHERE ("+where+") AND "+eligible+" ORDER BY "+order+" LIMIT 1",(*params,*eligibility_params)).fetchone()
        return row,0

    def verify(self, query):
        with self.lock:
            self._require_ready(verification=True)
            if self.thread and self.thread.is_alive():
                raise ValueError("采集任务尚未停止，请等待当前请求结束。")
            self.preview_data=None
            cutoff=int(time.time())-self.settings["refresh_days"]*86400
            unavailable=[]
            for attempt in range(3):
                row,job_id=self._verification_candidate(query,cutoff,[item["gid"] for item in unavailable])
                if row is None:
                    if unavailable:break
                    return {"verified":self.verified,"no_request":True,"message":"当前条件没有需要抓取的作品，未向源站发送请求。可先预估查看跳过数量。"}
                delay=max(0,self.settings["interval"]-(time.monotonic()-self.last_request))
                if not attempt and delay:
                    raise ValueError("距离上次请求太近，请稍后再验证。")
                if delay:time.sleep(delay)
                self.last_request=time.monotonic()
                self.verified=False
                try:
                    count=self._fetch(self.settings.copy(),row["gid"],row["token"])
                except CollectionPaused as error:
                    self.cooldown_until=time.time()+error.cooldown
                    self.store.remember_cooldown(self.cooldown_until)
                    raise ValueError(str(error)) from None
                except GalleryUnavailable as error:
                    with self.store.connection() as db:
                        task=db.execute("SELECT attempts FROM tasks WHERE job_id=? AND gid=?",(job_id,row["gid"])).fetchone() if job_id else None
                        attempts=(task[0] if task else 0)+1
                        if job_id:
                            db.execute("UPDATE tasks SET state='failed',attempts=?,error=? WHERE job_id=? AND gid=?",(attempts,str(error),job_id,row["gid"]))
                        self.store._save_failure(db,row["gid"],row["token"],self.settings["host"],error,attempts,job_id)
                    unavailable.append({"gid":row["gid"],"url":gallery_url(self.settings["host"],row["gid"],row["token"]),"error":str(error)})
                    continue
                except RetryableFetch as error:
                    raise ValueError(str(error)) from None
                finally:
                    self.last_request=time.monotonic()
                self.store.save(row["gid"],count,self.settings["host"])
                self.verified=True
                return {"verified":True,"gid":row["gid"],"favorite_count":count,"unavailable":unavailable}
            last=unavailable[-1]
            raise ValueError(f"本次验证的 {len(unavailable)} 条作品均不可访问，已记录到失败列表；这不表示源站登录失效。请核对具体作品，或再次验证其他待处理作品：{last['url']}")

    def _plan(self, query, operation, cutoff):
        if not self.settings:
            raise ValueError("请先应用采集设置。")
        if self.thread and self.thread.is_alive():
            raise ValueError("请等待当前采集请求结束，再预估任务。")
        if operation not in {"start","next_batch","next_round","resume","retry"}:
            raise ValueError("无效的采集操作。")
        job = self.store.latest()
        if operation in {"start","next_batch"}:
            if job and job["state"] in {"running","paused","preparing","awaiting_next"}:
                raise ValueError("请先继续或结束当前任务，再建立新任务。")
            if operation=="next_batch":
                if not job or job["collection_mode"]!="batch" or self.settings.get("collection_mode")!="batch":
                    raise ValueError("请先完成一批采集，并使用分批模式追加下一批。")
                query=job["query"]
            canonical,where,params = self.catalog.collection_plan(query)
            source = "catalog.gallery AS g LEFT JOIN main.favorites AS f ON f.gid=g.gid"
        else:
            allowed = {"awaiting_next"} if operation=="next_round" else {"paused","awaiting_next","completed_with_errors"} if operation=="retry" else {"paused"}
            if not job or job["state"] not in allowed:
                raise ValueError("当前没有可继续或重试的任务。")
            if operation=="next_round" and (job["collection_mode"]!="rounds" or job["current_round"]>=job["round_progress"]["total"]):
                raise ValueError("当前没有可继续的下一轮。")
            if job["host"] != self.settings["host"]:
                raise ValueError("请切换回创建任务时使用的源站。")
            canonical = job["query"]
            states = "('pending','failed')" if operation=="retry" else "('pending')"
            where,params = f"t.job_id=? AND t.state IN {states}",(job["id"],)
            if job["collection_mode"]=="rounds":
                if operation=="next_round":
                    where+=" AND t.round_no=?"
                    params+=(job["current_round"]+1,)
                else:
                    where+=" AND t.round_no<=?"
                    params+=(job["current_round"],)
            source = "tasks AS t LEFT JOIN main.favorites AS f ON f.gid=t.gid"
        eligible,extra = self._eligibility(cutoff)
        candidates=None
        mode=self.settings.get("collection_mode","full") if operation in {"start","next_batch"} else job["collection_mode"]
        rating_priority=self.settings.get("rating_priority",False) if operation in {"start","next_batch"} else bool(job.get("selection",{}).get("rating_priority",False))
        with self.store.connection() as db:
            db.execute("ATTACH DATABASE ? AS catalog",(self.catalog.path.as_uri()+"?mode=ro",))
            total = db.execute("SELECT COUNT(*) FROM " + source + " WHERE " + where,params).fetchone()[0]
            pending,never_seen = db.execute("SELECT COUNT(*),SUM(f.gid IS NULL) FROM " + source + " WHERE ("+where+") AND "+eligible,(*params,*extra)).fetchone()
            eligible_total=pending
            skipped=total-pending
            known=db.execute("SELECT COUNT(f.gid) FROM "+source+" WHERE "+where,params).fetchone()[0]
            if operation in {"start","next_batch"} and mode in {"batch","rounds"}:
                candidates=self._batch_candidates(db,where,params,eligible,extra,self.settings["batch_size"],rating_priority)
                pending=len(candidates)
                never_seen=sum(not item["known"] for item in candidates)
            years={}
            for item in candidates or []:years[item["year"]]=years.get(item["year"],0)+1
        selection=job.get("selection",{}) if job else {}
        new_task=operation in {"start","next_batch"}
        round_number=1 if new_task else job["current_round"]+(operation=="next_round")
        queue_total=eligible_total if new_task else job["total"]
        round_count=math.ceil(queue_total/self.settings["batch_size"]) if new_task and mode=="rounds" else selection.get("round_count",0)
        deferred=eligible_total-pending if new_task else (job["round_progress"]["remaining"]-(total if operation=="next_round" else 0)) if mode=="rounds" else 0
        return {"canonical":canonical,"where":where,"params":params,"eligible":eligible,"eligibility_params":extra,"total":total,"pending":pending,"skipped":skipped,"never_seen":never_seen or 0,"refresh_count":pending-(never_seen or 0),"job_id":job["id"] if operation!="start" else None,
                "collection_mode":mode,"batch_size":self.settings["batch_size"] if operation in {"start","next_batch"} else selection.get("batch_size"),
                "scope_total":total if operation in {"start","next_batch"} else selection.get("scope_total",job["total"]),
                "scope_known":known if new_task else None,"eligible_total":eligible_total,"deferred":deferred,
                "queue_total":queue_total,"round_count":round_count,"round_number":round_number,
                "candidates":candidates,"candidate_signature":self._candidate_signature(candidates),"year_breakdown":[{"year":year,"count":count} for year,count in sorted(years.items(),reverse=True)],"rating_priority":rating_priority}

    def preview(self, query, operation="start"):
        with self.lock:
            if not self.settings:
                raise ValueError("请先应用采集设置。")
            cutoff = int(time.time())-self.settings["refresh_days"]*86400
            plan = self._plan(query,operation,cutoff)
            if operation in {"start","next_batch"} and not plan["total"]:
                raise ValueError("当前搜索没有匹配作品。")
            samples = list(self.response_times)
            response_seconds = sum(samples)/len(samples) if samples else 2.0
            interval = self.settings["interval"]
            identifier = secrets.token_urlsafe(24)
            self.preview_data = {**plan,"id":identifier,"operation":operation,"cutoff":cutoff,"settings":self.settings.copy(),"expires":time.monotonic()+600}
            return {
                "preview_id":identifier,"operation":operation,"total":plan["total"],"pending":plan["pending"],"skipped":plan["skipped"],
                "never_seen":plan["never_seen"],"refresh_count":plan["refresh_count"],"interval":interval,"refresh_days":self.settings["refresh_days"],"skip_existing":self.settings["skip_existing"],
                "interval_seconds":math.ceil(plan["pending"]*interval),"estimated_seconds":math.ceil(plan["pending"]*(interval+response_seconds)),
                "response_seconds":round(response_seconds,3),"response_samples":len(samples),"estimate_basis":"measured" if samples else "assumed",
                "expires_at":int(time.time())+600,"query":plan["canonical"],"host":self.settings["host"],
                **{key:plan[key] for key in ("collection_mode","batch_size","scope_total","scope_known","eligible_total","deferred","year_breakdown","rating_priority","queue_total","round_count","round_number")},
            }

    def _confirmed_plan(self, query, preview_id, operation):
        preview = self.preview_data
        if not preview or not isinstance(preview_id,str) or not secrets.compare_digest(preview["id"],preview_id) or preview["operation"]!=operation:
            raise ValueError("请先预估耗时，再点击确认采集。")
        if time.monotonic()>preview["expires"] or self.settings!=preview["settings"]:
            self.preview_data = None
            raise ValueError("预估已过期或设置已变化，请重新预估。")
        plan = self._plan(query,operation,preview["cutoff"])
        if any(plan[key]!=preview[key] for key in ("canonical","total","pending","skipped","never_seen","job_id","candidate_signature","collection_mode","batch_size","rating_priority","round_number","queue_total")):
            self.preview_data = None
            raise ValueError("搜索范围、缓存或队列已变化，请重新预估后确认。")
        if plan["pending"]:
            self._require_ready()
        self.preview_data = None
        return plan

    def start(self, query, preview_id=None, operation="start"):
        with self.lock:
            if operation not in {"start","next_batch"}:raise ValueError("无效的新任务操作。")
            plan = self._confirmed_plan(query,preview_id,operation)
            canonical,where,params = plan["canonical"],plan["where"],plan["params"]
            now = int(time.time())
            with self.store.connection() as db:
                db.execute("ATTACH DATABASE ? AS catalog", (self.catalog.path.as_uri()+"?mode=ro",))
                batched=plan["collection_mode"]=="batch"
                rounds=plan["collection_mode"]=="rounds"
                total = plan["queue_total"] if rounds else plan["pending"] if batched else plan["total"]
                selection={key:plan[key] for key in ("collection_mode","batch_size","scope_total","scope_known","eligible_total","deferred","year_breakdown","rating_priority","round_count")}
                selection.update(scope_skipped=plan["skipped"],**{key:self.settings[key] for key in ("interval","refresh_days","skip_existing")})
                cursor = db.execute("INSERT INTO jobs(query_json,host,state,total,created_at,updated_at,selection_json) VALUES (?,?,'running',?,?,?,?)", (json.dumps(canonical,ensure_ascii=False),self.settings["host"],total,now,now,json.dumps(selection,ensure_ascii=False)))
                job_id = cursor.lastrowid
                if rounds:
                    # Freeze the full queue in SQLite without loading a large Tag into Python memory.
                    sql=self._candidate_sql(where,plan["eligible"],plan["rating_priority"])
                    db.execute("INSERT INTO tasks(job_id,gid,token,priority,round_no) SELECT ?,gid,token,priority,CAST(priority / ? AS INTEGER)+1 FROM ("+sql+")",(job_id,plan["batch_size"],*params,*plan["eligibility_params"]))
                elif batched:
                    db.executemany("INSERT INTO tasks(job_id,gid,token,priority) VALUES (?,?,?,?)",[(job_id,item["gid"],item["token"],index) for index,item in enumerate(plan["candidates"])])
                elif plan["rating_priority"]:
                    db.execute("""INSERT INTO tasks(job_id,gid,token,priority)
                        SELECT ?,g.gid,g.token,ROW_NUMBER() OVER (
                            ORDER BY EXISTS(SELECT 1 FROM main.tasks AS previous WHERE previous.gid=g.gid AND previous.attempts>0) ASC,
                                     CAST(g.rating AS REAL) DESC,g.gid DESC
                        )
                        FROM catalog.gallery AS g LEFT JOIN main.favorites AS f ON f.gid=g.gid
                        WHERE ("""+where+") AND "+plan["eligible"],(job_id,*params,*plan["eligibility_params"]))
                else:
                    db.execute("INSERT INTO tasks(job_id,gid,token) SELECT ?,g.gid,g.token FROM catalog.gallery AS g LEFT JOIN main.favorites AS f ON f.gid=g.gid WHERE (" + where + ") AND "+plan["eligible"], (job_id,*params,*plan["eligibility_params"]))
                pending = db.execute("SELECT COUNT(*) FROM tasks WHERE job_id=?", (job_id,)).fetchone()[0]
                db.execute("UPDATE jobs SET cached=?,state=?,message=? WHERE id=?", (total-pending,"running" if pending else "completed","正在采集收藏数。" if pending else "按当前跳过规则，全部作品均可复用已有数据，无需发送请求。",job_id))
            if pending:
                self._launch(job_id)
        return self.status()

    def next_batch(self,preview_id=None):
        return self.start({},preview_id,operation="next_batch")

    def next_round(self,preview_id=None):
        with self.lock:
            plan=self._confirmed_plan({},preview_id,"next_round")
            with self.store.connection() as db:
                self._skip_cached(db,plan)
                db.execute("UPDATE jobs SET current_round=?,state='running',message='正在采集下一轮。',updated_at=? WHERE id=?",(plan["round_number"],int(time.time()),plan["job_id"]))
            self._launch(plan["job_id"])
        return self.status()

    @staticmethod
    def _skip_cached(db,plan):
        db.execute("UPDATE tasks SET state='skipped',error='' WHERE job_id=? AND gid IN (SELECT t.gid FROM tasks AS t LEFT JOIN main.favorites AS f ON f.gid=t.gid WHERE ("+plan["where"]+") AND NOT "+plan["eligible"]+")",(plan["job_id"],*plan["params"],*plan["eligibility_params"]))

    def _launch(self, job_id):
        self.interrupt.clear()
        self.thread = threading.Thread(target=self._run, args=(job_id,self.settings.copy()), daemon=True, name="favorite-collector")
        self.thread.start()

    def pause(self, cancel=False):
        with self.lock:
            self.preview_data = None
            self.interrupt.set()
            with self.store.connection() as db:
                db.execute("UPDATE jobs SET state=?,message=?,updated_at=? WHERE id=(SELECT MAX(id) FROM jobs) AND state IN "+("('running','paused','awaiting_next')" if cancel else "('running','paused')"), ("cancelled" if cancel else "paused","任务已结束，已采集数据保留。" if cancel else "任务已暂停；当前请求若已发出，会等待它结束。",int(time.time())))
        return self.status()

    def resume(self, retry_failed=False, preview_id=None):
        with self.lock:
            operation = "retry" if retry_failed else "resume"
            plan = self._confirmed_plan({},preview_id,operation)
            job = self.store.latest()
            with self.store.connection() as db:
                self._skip_cached(db,plan)
                if retry_failed:
                    db.execute("UPDATE tasks SET state='pending',attempts=0,error='' WHERE job_id=? AND state='failed' AND round_no<=?", (job["id"],job["current_round"]))
                db.execute("UPDATE jobs SET state='running',message='正在继续采集。',updated_at=? WHERE id=?", (int(time.time()),job["id"]))
            self._launch(job["id"])
        return self.status()

    def _run(self, job_id, settings):
        try:
            with self.store.connection() as db:
                job=db.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
                selection=json.loads(job["selection_json"])
                rounds=selection.get("collection_mode")=="rounds"
                round_limit=job["current_round"] if rounds else 1
            while not self.interrupt.is_set():
                with self.store.connection() as db:
                    row = db.execute("SELECT * FROM tasks WHERE job_id=? AND state='pending' AND round_no<=? ORDER BY priority ASC,gid DESC LIMIT 1", (job_id,round_limit)).fetchone()
                    if row is None:
                        failed = db.execute("SELECT COUNT(*) FROM tasks WHERE job_id=? AND state='failed'", (job_id,)).fetchone()[0]
                        batch=selection.get("collection_mode")=="batch"
                        message=("本批已处理完，有失败项；可以重试或追加下一批。" if failed else "本批采集完成，可查看已记录作品，或预估并追加下一批。") if batch else ("任务已处理完，但有失败项；可以重试失败项。" if failed else "所有待采集项均已成功，收藏数已保存。")
                        has_next=rounds and round_limit<selection["round_count"]
                        if rounds:
                            message=f"第 {round_limit} 轮已处理完。"+("已自动停止，进度已保存；点击“继续下一轮”后才会继续。" if has_next else "全部轮次已处理完，进度已保存。")+(f"有 {failed} 条失败项，可单独重试。" if failed else "")
                        updated=db.execute("UPDATE jobs SET state=?,message=?,updated_at=? WHERE id=? AND state='running'", ("awaiting_next" if has_next else "completed_with_errors" if failed else "completed",message,int(time.time()),job_id)).rowcount
                if row is None:
                    if updated and not has_next and self.on_completed:
                        try:self.on_completed(job_id)
                        except Exception:pass
                    return
                delay = max(0, settings["interval"] - (time.monotonic()-self.last_request))
                if self.interrupt.wait(delay):
                    return
                try:
                    count = self._fetch(settings, row["gid"], row["token"])
                    with self.store.connection() as db:
                        self.store._save(db,row["gid"],count,settings["host"])
                        db.execute("UPDATE tasks SET state='done',attempts=attempts+1,error='' WHERE job_id=? AND gid=?", (job_id,row["gid"]))
                        db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (int(time.time()),job_id))
                except CollectionPaused as error:
                    with self.lock:
                        self.cooldown_until = int(time.time()+error.cooldown)
                        self.store.remember_cooldown(self.cooldown_until)
                        with self.store.connection() as db:
                            db.execute("UPDATE jobs SET state='paused',message=?,cooldown_until=?,updated_at=? WHERE id=? AND state='running'", (str(error),self.cooldown_until,int(time.time()),job_id))
                    return
                except (RetryableFetch, GalleryUnavailable) as error:
                    attempts = row["attempts"]+1
                    terminal = isinstance(error, GalleryUnavailable) or attempts >= 3
                    with self.store.connection() as db:
                        db.execute("UPDATE tasks SET state=?,attempts=?,error=? WHERE job_id=? AND gid=?", ("failed" if terminal else "pending",attempts,str(error),job_id,row["gid"]))
                        if terminal:self.store._save_failure(db,row["gid"],row["token"],settings["host"],error,attempts,job_id)
                        db.execute("UPDATE jobs SET message=?,updated_at=? WHERE id=? AND state='running'", (str(error) + (" 已记录失败项。" if terminal else " 正在等待后重试。"),int(time.time()),job_id))
                    if not terminal and self.interrupt.wait(max(settings["interval"],10*attempts)):
                        return
                finally:
                    self.last_request = time.monotonic()
        except Exception:
            with self.store.connection() as db:
                db.execute("UPDATE jobs SET state='paused',message='采集遇到内部错误，已暂停；已有收藏数保留。',updated_at=? WHERE id=? AND state='running'", (int(time.time()),job_id))

    def close(self):
        self.pause()
        if self.thread:
            self.thread.join(timeout=30)
