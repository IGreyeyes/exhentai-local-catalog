"""Opt-in anonymous submission and revisioned public corrections."""
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import json
import re
import secrets
import threading
import time

from collector_identity import read_identity, validate_collector

DEFAULT_ENDPOINT = "https://excatalog-sync-api.greyeyes.workers.dev"
FORMAT = "excatalog-cloud-sync"
VERSION = 3
FIELDS = {"gid", "favorite_count", "rating_count", "checked_at", "source", "collector_id", "collector_name"}
MAX_COUNT = 1_000_000_000


def encode_snapshot(item):
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_endpoint(value):
    if not isinstance(value, str) or len(value) > 250:
        raise ValueError("同步服务地址不正确。")
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment or parts.port not in {None, 443}
            or not re.fullmatch(r"[a-zA-Z0-9.-]+", parts.hostname)):
        raise ValueError("同步服务须填写 HTTPS 根地址，不包含账号、路径或查询参数。")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("同步服务发生跳转，已停止请求；请核对服务地址。")


def request_json(endpoint, path, payload=None, token=""):
    headers = {"Accept": "application/json", "User-Agent": "ExCatalog-CloudSync/3"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with build_opener(NoRedirect()).open(Request(endpoint + path, data=data, headers=headers), timeout=25) as response:
            if response.headers.get_content_type() != "application/json":
                raise ValueError("同步服务没有返回 JSON，请检查 Worker 代码和服务地址。")
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("同步响应过大，已停止接收。")
        return json.loads(raw.decode("utf-8"))
    except HTTPError as error:
        status = error.code
        retry_after = error.headers.get("Retry-After", "")
        quota_scope = error.headers.get("X-Upload-Limit-Scope", "")
        remaining = error.headers.get("X-Upload-Remaining", "")
        error.close()
        if status == 429:
            delay = min(86400, max(1, int(retry_after))) if retry_after.isdecimal() else 300
            batch_limit = int(remaining) if remaining.isdecimal() and 0 < int(remaining) < 50 and quota_scope == "day" else None
            raise SyncRateLimited("今日上传额度已用完，等待额度重置。" if quota_scope == "day"
                                  else "同步服务限流，按服务器给出的时间重试。", delay, batch_limit) from None
        if status == 409 and path.startswith("/v3/changes"):
            raise SyncReset("公共库已更换或恢复，正在重新下载。") from None
        if status == 401 and path in {"/v3/contribute", "/v3/upload-check"}:
            raise UploadIdentityMissing("上传身份需重新登记，稍后重试。") from None
        if status == 403 and path in {"/v3/register", "/v3/contribute", "/v3/upload-check"}:
            raise UploadBlocked("该上传来源已被管理员封禁；公共下载继续可用。") from None
        messages = {401: "上传身份未登记或不匹配，请检查同步身份设置。", 403: "该上传来源已被管理员封禁；公共下载继续可用。", 426: "同步服务需要新版客户端，请更新后重试。", 429: "同步服务限流，稍后自动重试。",
                    400: "同步数据或采集身份不符合服务要求。",
                    404: "同步服务尚未支持资料库全量上传，请维护者部署 v1.1.19 的 worker.js（v3）后重试。",
                    503: "上传可能已暂停、候选容量已满或服务尚未就绪，请检查云端管理页。"}
        if status == 400 and path == "/v3/register" and payload is not None and "uploader_name" in payload:
            raise ValueError("上传者昵称登记未完成；请维护者部署 v1.1.23 或更新版本的 Worker，并检查昵称与采集身份。昵称及上传队列已保留。") from None
        raise ValueError(messages.get(status, f"同步服务返回 HTTP {status}，稍后重试。")) from None
    except (URLError, OSError, TimeoutError):
        raise ValueError("同步网络连接失败；本地资料保留，稍后自动重试。") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("同步响应格式不正确，本地资料保留。") from None


class SyncReset(ValueError):
    pass


class UploadIdentityMissing(ValueError):
    pass


class UploadBlocked(ValueError):
    pass


class SyncRateLimited(ValueError):
    def __init__(self, message, retry_after=300, batch_limit=None):
        super().__init__(message)
        self.retry_after, self.batch_limit = retry_after, batch_limit


class CloudSync:
    def __init__(self, store_provider, gate, maintenance_busy=lambda: False, transport=request_json, start=True):
        self.store_provider, self.gate, self.maintenance_busy = store_provider, gate, maintenance_busy
        self.transport = transport
        self.lock = threading.RLock()
        self.wake, self.stopped = threading.Event(), threading.Event()
        self.busy = False
        self.message = "在线同步尚未启用。"
        self.phase = "idle"
        self.last_success = None
        self.downloaded = self.uploaded = self.pending = 0
        self.received = 0
        self.upload_remaining = None
        self.upload_total = self.upload_confirmed = None
        self.session_upload_processed = self.session_download_received = 0
        self.next_due = 0
        self.upload_batch_size = 5
        self.capabilities_endpoint = None
        self.capabilities_refresh_at = 0
        self.upload_min_interval_days = None
        self.upload_retry_until = 0
        self.preflight_enabled = False
        self.preflight_needed = {}
        self.session_preflight_checked = 0
        self._seed_self()
        with self.gate, self.store_provider().connection() as db:
            if self._get(db, "cloud_protocol") not in {"2", str(VERSION)}:
                self._put(db, "cloud_cursor", 0)
                self._put(db, "cloud_dataset", "")
                db.execute("DELETE FROM cloud_upload_ack")
            self._put(db, "cloud_protocol", VERSION)
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._loop, daemon=True, name="cloud-sync")
            self.thread.start()

    def _seed_self(self):
        with self.gate, self.store_provider().connection() as db:
            if db.execute("SELECT 1 FROM app_settings WHERE key='cloud_self_seeded'").fetchone():
                return
            own = read_identity(db)["collector_id"]
            for row in db.execute("""SELECT f.gid,f.favorite_count,f.checked_at,f.source,f.collector_id,f.collector_name,
                CASE WHEN r.checked_at=f.checked_at AND r.source=f.source THEN r.rating_count END AS rating_count
                FROM favorites f LEFT JOIN rating_counts r ON r.gid=f.gid WHERE f.collector_id=?""", (own,)):
                item = dict(row)
                db.execute("INSERT OR IGNORE INTO cloud_self_snapshots VALUES(?,?,?)", (item["gid"], own, encode_snapshot(item)))
            db.execute("INSERT INTO app_settings VALUES('cloud_self_seeded','1')")

    @staticmethod
    def _get(db, key, default=""):
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def _put(db, key, value):
        db.execute("INSERT INTO app_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def _config(self):
        with self.gate, self.store_provider().connection() as db:
            download = self._get(db, "cloud_download_enabled", self._get(db, "cloud_enabled", "0")) == "1"
            upload = self._get(db, "cloud_upload_enabled", "0") == "1"
            return {"endpoint": self._get(db, "cloud_endpoint", DEFAULT_ENDPOINT),
                    "uploader_name": self._get(db, "cloud_uploader_name"),
                    "enabled": download or upload, "download_enabled": download, "upload_enabled": upload,
                    "configured": bool(db.execute("SELECT 1 FROM app_settings WHERE key IN ('cloud_enabled','cloud_download_enabled')").fetchone())}

    def status(self):
        config = self._config()
        source_id, source_status = self._identity_status()
        skipped = self._skip_count()
        with self.lock:
            return {**config, "busy": self.busy, "source_id": source_id, "source_status": source_status, "message": self.message,
                    "last_success": self.last_success, "downloaded": self.downloaded, "uploaded": self.uploaded,
                    "pending": self.pending, "interval_minutes": 15, "phase": self.phase,
                    "upload_remaining": self.upload_remaining, "session_upload_processed": self.session_upload_processed,
                    "upload_total": self.upload_total, "upload_confirmed": self.upload_confirmed,
                    "upload_batch_size": self.upload_batch_size,
                    "upload_preflight_enabled": self.preflight_enabled,
                    "upload_min_interval_days": self.upload_min_interval_days,
                    "upload_skipped": skipped, "session_preflight_checked": self.session_preflight_checked,
                    "upload_retry_at": int(time.time()+self.upload_retry_until-time.monotonic())
                        if self.upload_retry_until > time.monotonic() else None,
                    "session_download_received": self.session_download_received,
                    "next_sync_at": int(time.time()+max(0,self.next_due-time.monotonic()))
                        if config["enabled"] and not self.busy and self.next_due else None}

    def configure(self, value):
        # Accept the older local API field as a download preference during upgrades.
        if set(value) - {"uploader_name"} == {"endpoint", "enabled", "upload_enabled", "consent"}:
            value = {**value, "download_enabled": value["enabled"]}
            del value["enabled"]
        if set(value) - {"uploader_name"} != {"endpoint", "download_enabled", "upload_enabled", "consent"}:
            raise ValueError("在线同步设置字段不正确。")
        if "uploader_name" in value:
            name = value["uploader_name"]
            if not isinstance(name, str):
                raise ValueError("上传者昵称必须是文本。")
            name = name.strip()
            validate_collector("00000000-0000-4000-8000-000000000000", name)
        endpoint = validate_endpoint(value["endpoint"])
        if any(type(value[key]) is not bool for key in ("download_enabled", "upload_enabled", "consent")):
            raise ValueError("同步选项须为开关值。")
        if value["upload_enabled"] and not value["consent"]:
            raise ValueError("启用上传前，请确认公开分享资料库中的全部成功收藏数记录及原采集者信息。")
        enabled = value["download_enabled"] or value["upload_enabled"]
        with self.gate, self.lock:
            if self.busy or self.maintenance_busy():
                raise ValueError("请等待同步或维护结束后再修改设置。")
            with self.store_provider().connection() as db:
                previous = self._get(db, "cloud_endpoint", DEFAULT_ENDPOINT)
                if previous != endpoint:
                    self._clear_identity(db)
                    db.execute("DELETE FROM cloud_applied")
                    self._put(db, "cloud_cursor", 0)
                    self._put(db, "cloud_dataset", "")
                    db.execute("DELETE FROM cloud_upload_ack")
                for key in ("download_enabled", "upload_enabled"):
                    self._put(db, "cloud_" + key, "1" if value[key] else "0")
                self._put(db, "cloud_enabled", "1" if enabled else "0")
                self._put(db, "cloud_endpoint", endpoint)
                if "uploader_name" in value:
                    self._put(db, "cloud_uploader_name", name)
            self.message = ("设置及上传者昵称已保存；开启上传后同步到后台，新来源的提交先待审核。"
                            if enabled else "设置及上传者昵称已保存；在线同步已关闭，开启上传后同步昵称。")
            self.phase = "queued" if enabled else "idle"
            self.upload_remaining = None
            self.upload_total = self.upload_confirmed = None
            self.next_due = 0
            self.upload_retry_until = 0
            self.preflight_needed.clear()
            self.capabilities_refresh_at = 0
            self.wake.set()
        return self.status()

    def trigger(self):
        with self.gate, self.lock:
            if not self._config()["enabled"]:
                raise ValueError("请先保存并启用在线同步。")
            if self.busy or self.maintenance_busy():
                raise ValueError("同步或维护正在进行，请等待完成。")
            self.next_due = 0
            self.upload_retry_until = 0
            with self.store_provider().connection() as db:
                self._clear_skips(db)
            self.preflight_needed.clear()
            self.capabilities_refresh_at = 0
            self.phase = "queued"
            self.message = "已安排立即同步，将继续未完成的队列。"
            self.wake.set()
        return self.status()

    @staticmethod
    def _clear_identity(db):
        db.execute("DELETE FROM app_settings WHERE key IN ('cloud_upload_token','cloud_upload_owner','cloud_upload_endpoint','cloud_source_id','cloud_source_status','cloud_uploader_name_synced')")
        db.execute("DELETE FROM cloud_upload_skips")
        db.execute("DELETE FROM app_settings WHERE key IN ('cloud_preflight_dataset','cloud_preflight_cursor','cloud_preflight_interval_days')")

    def _identity_status(self):
        with self.gate, self.store_provider().connection() as db:
            return self._get(db, "cloud_source_id"), self._get(db, "cloud_source_status", "unregistered")

    def _upload_identity(self, config):
        with self.gate, self.store_provider().connection() as db:
            identity = read_identity(db)
            if (self._get(db, "cloud_upload_owner") != identity["collector_id"]
                    or self._get(db, "cloud_upload_endpoint") != config["endpoint"]):
                self._clear_identity(db)
                self.preflight_needed.clear()
                db.execute("DELETE FROM cloud_upload_ack")
            token = self._get(db, "cloud_upload_token")
            name_row = db.execute("SELECT value FROM app_settings WHERE key='cloud_uploader_name'").fetchone()
            name = name_row[0] if name_row else None
            name_synced = name is None or self._get(db, "cloud_uploader_name_synced", None) == name
            if token and self._get(db, "cloud_source_id") and name_synced:
                return token
            if not token:
                token = "src_" + secrets.token_urlsafe(32)
                self._put(db, "cloud_upload_token", token)
                self._put(db, "cloud_upload_owner", identity["collector_id"])
                self._put(db, "cloud_upload_endpoint", config["endpoint"])
        payload = {**identity, **({"uploader_name": name} if name is not None else {})}
        result = self.transport(config["endpoint"], "/v3/register", payload, token)
        if (not isinstance(result, dict) or set(result) != {"version", "source_id", "status"}
                or result["version"] != VERSION or result["status"] not in {"review", "trusted"}):
            raise ValueError("上传身份登记结果不正确。")
        validate_collector(result["source_id"], "")
        with self.gate, self.store_provider().connection() as db:
            self._put(db, "cloud_source_id", result["source_id"])
            self._put(db, "cloud_source_status", result["status"])
            if name is not None:
                self._put(db, "cloud_uploader_name_synced", name)
        return token

    def _available(self):
        if self.stopped.is_set() or self.maintenance_busy():
            raise ValueError("正在停止服务或维护资料库，同步已暂停。")

    def _refresh_upload_snapshots(self):
        """Refresh changed records only; triggers retain changes made during network requests."""
        with self.gate:
            self._available()
            with self.store_provider().connection() as db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute("""SELECT f.gid,f.favorite_count,f.checked_at,f.source,f.collector_id,f.collector_name,
                    CASE WHEN r.checked_at=f.checked_at AND r.source=f.source THEN r.rating_count END AS rating_count
                    FROM cloud_upload_dirty d JOIN favorites f ON f.gid=d.gid
                    LEFT JOIN rating_counts r ON r.gid=f.gid""").fetchall()
                db.executemany("""INSERT INTO cloud_upload_snapshots VALUES(?,?) ON CONFLICT(gid) DO UPDATE SET
                    payload=excluded.payload WHERE cloud_upload_snapshots.payload IS NOT excluded.payload""",
                    ((row["gid"], encode_snapshot(dict(row))) for row in rows))
                db.execute("""DELETE FROM cloud_upload_snapshots WHERE gid IN (SELECT gid FROM cloud_upload_dirty)
                    AND NOT EXISTS(SELECT 1 FROM favorites WHERE favorites.gid=cloud_upload_snapshots.gid)""")
                db.execute("DELETE FROM cloud_upload_dirty")
                total = db.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
        with self.lock:
            self.upload_total = total

    def _count_upload_queue(self):
        with self.gate:
            self._available()
            with self.store_provider().connection() as db:
                remaining = db.execute("SELECT COUNT(*) FROM cloud_upload_queue").fetchone()[0]
        with self.lock:
            self.upload_remaining = remaining
            self.upload_confirmed = self.upload_total - remaining if self.upload_total is not None else None
        return remaining

    def _skip_count(self):
        with self.gate, self.store_provider().connection() as db:
            return db.execute("""SELECT COUNT(*) FROM cloud_upload_skips s
                JOIN cloud_upload_snapshots o ON o.gid=s.gid AND o.payload=s.payload
                JOIN cloud_upload_ack a ON a.gid=s.gid AND a.payload=s.payload""").fetchone()[0]

    @staticmethod
    def _clear_skips(db):
        db.execute("""DELETE FROM cloud_upload_ack WHERE EXISTS(SELECT 1 FROM cloud_upload_skips s
            WHERE s.gid=cloud_upload_ack.gid AND s.payload=cloud_upload_ack.payload)""")
        db.execute("DELETE FROM cloud_upload_skips")

    @staticmethod
    def _invalidate_skip(db, gid, revision):
        row = db.execute("SELECT payload,revision FROM cloud_upload_skips WHERE gid=?", (gid,)).fetchone()
        if row and revision > row["revision"]:
            db.execute("DELETE FROM cloud_upload_ack WHERE gid=? AND payload=?", (gid, row["payload"]))
            db.execute("DELETE FROM cloud_upload_skips WHERE gid=?", (gid,))

    def _sync_preflight_state(self, config):
        with self.gate, self.store_provider().connection() as db:
            self._available()
            dataset = self._get(db, "cloud_preflight_dataset")
            cursor = int(self._get(db, "cloud_preflight_cursor", "0"))
            expired = db.execute("SELECT gid,payload FROM cloud_upload_skips WHERE verified_at<?", (int(time.time())-86400,)).fetchall()
            db.executemany("DELETE FROM cloud_upload_ack WHERE gid=? AND payload=?", ((r["gid"],r["payload"]) for r in expired))
            db.execute("DELETE FROM cloud_upload_skips WHERE verified_at<?", (int(time.time())-86400,))
        until = None
        for _ in range(50):
            self._available()
            params = {"after":cursor}
            if dataset:params["dataset_id"] = dataset
            if until is not None:params["until"] = until
            result = self.transport(config["endpoint"], "/v3/upload-state?"+urlencode(params))
            keys = {"version","dataset_id","reset","cursor","until","has_more","corrections"}
            if (not isinstance(result,dict) or set(result)!=keys or type(result["version"]) is not int or result["version"]!=VERSION
                or not isinstance(result["dataset_id"],str) or not re.fullmatch(r"[0-9a-f]{32}",result["dataset_id"])
                or type(result["reset"]) is not bool or type(result["has_more"]) is not bool
                or any(type(result[k]) is not int or not 0<=result[k]<=2**53-1 for k in ("cursor","until"))
                or result["cursor"]>result["until"] or not isinstance(result["corrections"],list) or len(result["corrections"])>500):
                raise ValueError("公共版本校验结果不正确，已保留上传队列。")
            if result["reset"]:
                if result["has_more"] or result["corrections"] or result["cursor"]!=result["until"]:
                    raise ValueError("公共库重置确认不正确。")
            elif (result["dataset_id"]!=dataset or not cursor<=result["cursor"]
                  or until is not None and result["until"]!=until
                  or result["has_more"] and result["cursor"]==cursor):
                raise ValueError("公共版本校验进度不正确。")
            revision = cursor
            for item in result["corrections"]:
                if (not isinstance(item,dict) or set(item)!={"gid","revision"}
                    or type(item["gid"]) is not int or not 1<=item["gid"]<=2**53-1
                    or type(item["revision"]) is not int or not revision<item["revision"]<=result["cursor"]):
                    raise ValueError("公共纠正记录不正确，已保留上传队列。")
                revision = item["revision"]
            if result["has_more"] and revision!=result["cursor"]:
                raise ValueError("公共纠正分页不正确。")
            if not result["has_more"] and result["cursor"]!=result["until"]:
                raise ValueError("公共校验尚未完成，已保留队列。")
            with self.gate, self.store_provider().connection() as db:
                self._available()
                if result["reset"]:
                    if dataset:db.execute("DELETE FROM cloud_upload_ack")
                    self._clear_skips(db)
                    self.preflight_needed.clear()
                else:
                    for item in result["corrections"]:
                        self._invalidate_skip(db,item["gid"],item["revision"])
                        self.preflight_needed.pop(item["gid"],None)
                dataset,cursor = result["dataset_id"],result["cursor"]
                self._put(db,"cloud_preflight_dataset",dataset)
                self._put(db,"cloud_preflight_cursor",cursor)
            if not result["has_more"]:return True
            until = result["until"]
        return False

    def _preflight_check(self, config, rows, token):
        records=[]
        for row in rows:
            item=json.loads(row["payload"])
            if (not isinstance(item,dict) or set(item)!=FIELDS or type(item["gid"]) is not int or item["gid"]!=row["gid"]
                or type(item["favorite_count"]) is not int or not 0<=item["favorite_count"]<=MAX_COUNT
                or item["rating_count"] is not None and (type(item["rating_count"]) is not int or not 0<=item["rating_count"]<=MAX_COUNT)
                or type(item["checked_at"]) is not int or not 1<=item["checked_at"]<=int(time.time())+300
                or item["source"] not in {"e-hentai.org","exhentai.org"}):
                raise ValueError("本地待上传快照不符合要求，已保留队列。")
            validate_collector(item["collector_id"],item["collector_name"],allow_unknown=True)
            records.append({"gid":item["gid"],"checked_at":item["checked_at"]})
        with self.lock:
            self.phase="checking_upload"
            self.message=f"正在检查云端差异：本次运行累计检查 {self.session_preflight_checked:,} 条摘要。"
        result=self.transport(config["endpoint"],"/v3/upload-check",{"version":VERSION,"records":records},token)
        expected={r["gid"]:r for r in rows}
        if (not isinstance(result,dict) or set(result)!={"version","dataset_id","source_id","status","records"}
            or type(result["version"]) is not int or result["version"]!=VERSION
            or not isinstance(result["dataset_id"],str) or not re.fullmatch(r"[0-9a-f]{32}",result["dataset_id"])
            or result["source_id"]!=self._identity_status()[0] or result["status"] not in {"trusted","review"}
            or not isinstance(result["records"],list) or len(result["records"])!=len(rows)):
            raise ValueError("上传差异检查结果不正确，已保留队列。")
        seen=set()
        for item in result["records"]:
            if (not isinstance(item,dict) or set(item)!={"gid","skip","revision"}
                or type(item["gid"]) is not int or item["gid"] not in expected or item["gid"] in seen
                or type(item["skip"]) is not bool or type(item["revision"]) is not int
                or not 0<=item["revision"]<=2**53-1 or item["skip"] and not item["revision"]):
                raise ValueError("上传差异检查记录不正确，已保留队列。")
            seen.add(item["gid"])
        with self.gate, self.store_provider().connection() as db:
            self._available()
            old_dataset=self._get(db,"cloud_preflight_dataset")
            if old_dataset and old_dataset!=result["dataset_id"]:
                db.execute("DELETE FROM cloud_upload_ack")
                self._clear_skips(db)
                self.preflight_needed.clear()
                self._put(db,"cloud_preflight_cursor",0)
            self._put(db,"cloud_preflight_dataset",result["dataset_id"])
            self._put(db,"cloud_source_status",result["status"])
            for item in result["records"]:
                row=expected[item["gid"]]
                if item["skip"]:
                    db.execute("INSERT INTO cloud_upload_ack VALUES(?,?) ON CONFLICT(gid) DO UPDATE SET payload=excluded.payload",(row["gid"],row["payload"]))
                    db.execute("""INSERT INTO cloud_upload_skips VALUES(?,?,?,?,?) ON CONFLICT(gid) DO UPDATE SET
                        payload=excluded.payload,dataset_id=excluded.dataset_id,revision=excluded.revision,verified_at=excluded.verified_at""",
                        (row["gid"],row["payload"],result["dataset_id"],item["revision"],int(time.time())))
                    self.preflight_needed.pop(row["gid"],None)
                else:self.preflight_needed[row["gid"]]=(row["payload"],time.monotonic()+300)
        with self.lock:self.session_preflight_checked+=len(rows)
        self._count_upload_queue()

    def _upload_progress(self):
        with self.lock:
            self.phase = "uploading"
            remaining = f"待上传 {self.upload_remaining:,} 条" if self.upload_remaining is not None else "正在检查待上传数量"
            self.message = f"正在上传资料库收藏数：本次运行累计已提交 {self.session_upload_processed:,} 条，{remaining}。"

    def _download_progress(self):
        with self.lock:
            self.phase = "downloading"
            self.message = f"正在下载公共数据：本轮已接收 {self.received:,} 条，本地新增或更新 {self.downloaded:,} 条。"

    def _upload(self, config):
        if not config["upload_enabled"]:
            with self.lock:
                self.upload_remaining = None
                self.upload_total = self.upload_confirmed = None
            return False
        if self.upload_retry_until > time.monotonic():
            raise SyncRateLimited("上传仍在等待服务器额度重置或解除限流。",
                                  max(1,int(self.upload_retry_until-time.monotonic())))
        self._refresh_upload_snapshots()
        if not self.upload_total:
            if self._identity_status()[0]:
                self._upload_identity(config)
            self._count_upload_queue()
            return False
        token = self._upload_identity(config)
        if not self._count_upload_queue() and not self._skip_count():
            return False
        if self.capabilities_endpoint != config["endpoint"] or time.monotonic() >= self.capabilities_refresh_at:
            self.upload_batch_size = 5
            self.preflight_enabled = False
            self.upload_min_interval_days = None
            try:
                capabilities = self.transport(config["endpoint"], "/health")
                size = capabilities.get("max_upload_batch") if isinstance(capabilities, dict) else None
                if isinstance(capabilities, dict) and capabilities.get("version") == VERSION and type(size) is int and 5 <= size <= 50:
                    self.upload_batch_size = size
                    self.preflight_enabled = (capabilities.get("upload_preflight") is True
                        and capabilities.get("public_correction_state") is True
                        and type(capabilities.get("max_upload_check_batch")) is int
                        and capabilities.get("max_upload_check_batch") == 500)
                    days = capabilities.get("upload_min_interval_days")
                    if self.preflight_enabled and type(days) is int and 0 <= days <= 365:
                        self.upload_min_interval_days = days
                    with self.gate, self.store_provider().connection() as db:
                        policy_key = "cloud_preflight_interval_days"
                        policy_value = str(self.upload_min_interval_days) if self.upload_min_interval_days is not None else "legacy"
                        previous = self._get(db, policy_key)
                        if previous and previous != policy_value:
                            self._clear_skips(db)
                            self.preflight_needed.clear()
                        self._put(db, policy_key, policy_value)
                self.capabilities_endpoint = config["endpoint"]
                self.capabilities_refresh_at = time.monotonic()+300
            except ValueError:
                if self._skip_count():raise ValueError("暂时无法核对服务能力，免上传确认及本地资料保留，稍后重试。") from None
        if self.preflight_enabled:
            if not self._sync_preflight_state(config):return True
        elif self._skip_count():
            with self.gate, self.store_provider().connection() as db:self._clear_skips(db)
            self.preflight_needed.clear()
        if not self._count_upload_queue():return False
        for _ in range(20):
            self._available()
            batch_size = self.upload_batch_size
            if self.preflight_enabled:
                with self.gate, self.store_provider().connection() as db:
                    candidates = db.execute("SELECT gid,payload FROM cloud_upload_queue ORDER BY gid LIMIT 1000").fetchall()
                if not candidates:
                    self._refresh_upload_snapshots()
                    return self._count_upload_queue()>0
                def already_checked(row):
                    cached=self.preflight_needed.get(row["gid"])
                    return bool(cached and cached[0]==row["payload"] and cached[1]>time.monotonic())
                prefix=[]
                for row in candidates[:batch_size]:
                    if not already_checked(row):break
                    prefix.append(row)
                if len(prefix)<batch_size and len(candidates)>len(prefix):
                    unchecked=[r for r in candidates if not already_checked(r)][:500]
                    try:self._preflight_check(config,unchecked,token)
                    except UploadIdentityMissing:
                        with self.gate, self.store_provider().connection() as db:
                            db.execute("DELETE FROM app_settings WHERE key='cloud_source_id'")
                        raise
                    if self.stopped.wait(2.1):return False
                    continue
                batch_size=len(prefix)
            self._upload_progress()
            while True:
                self._available()
                with self.gate, self.store_provider().connection() as db:
                    rows = db.execute("SELECT gid,payload FROM cloud_upload_queue ORDER BY gid LIMIT ?",
                                      (batch_size,)).fetchall()
                if not rows:
                    self._refresh_upload_snapshots()
                    return self._count_upload_queue() > 0
                items = [json.loads(row["payload"]) for row in rows]
                try:
                    result = self.transport(config["endpoint"], "/v3/contribute", {"version": VERSION, "records": items}, token)
                    break
                except SyncRateLimited as error:
                    if error.batch_limit is not None and 0 < error.batch_limit < len(items):
                        batch_size = error.batch_limit
                        continue
                    raise
                except UploadIdentityMissing:
                    with self.gate, self.store_provider().connection() as db:
                        db.execute("DELETE FROM app_settings WHERE key='cloud_source_id'")
                    raise
            if (not isinstance(result, dict) or result.get("version") != VERSION
                    or any(type(result.get(key)) is not int or not 0 <= result[key] <= len(items) for key in ("accepted", "pending", "kept"))
                    or sum(result[key] for key in ("accepted", "pending", "kept")) != len(items)):
                raise ValueError("上传结果不正确，保留本地队列以便重试。")
            skipped = result.get("upload_skips", [])
            row_by_gid = {row["gid"]:row for row in rows}
            if (not isinstance(skipped,list) or len(skipped)>result["kept"]
                or any(not isinstance(item,dict) or set(item)!={"gid","revision"}
                    or type(item["gid"]) is not int or item["gid"] not in row_by_gid
                    or type(item["revision"]) is not int or not 1<=item["revision"]<=2**53-1 for item in skipped)
                or len({item["gid"] for item in skipped})!=len(skipped)
                or skipped and (not isinstance(result.get("dataset_id"),str)
                    or not re.fullmatch(r"[0-9a-f]{32}",result["dataset_id"])
                    or type(result.get("upload_min_interval_days")) is not int
                    or not 0<=result["upload_min_interval_days"]<=365)):
                raise ValueError("提交时的免上传确认不正确，已保留本地队列。")
            with self.gate:
                self._available()
                with self.store_provider().connection() as db:
                    if skipped:
                        old_dataset=self._get(db,"cloud_preflight_dataset")
                        if old_dataset and old_dataset!=result["dataset_id"]:
                            db.execute("DELETE FROM cloud_upload_ack")
                            self._clear_skips(db)
                            self._put(db,"cloud_preflight_cursor",0)
                            self.preflight_needed.clear()
                        self._put(db,"cloud_preflight_dataset",result["dataset_id"])
                        self._put(db,"cloud_preflight_interval_days",result["upload_min_interval_days"])
                    db.executemany("INSERT INTO cloud_upload_ack VALUES(?,?) ON CONFLICT(gid) DO UPDATE SET payload=excluded.payload",
                                   ((row["gid"], row["payload"]) for row in rows))
                    db.executemany("DELETE FROM cloud_upload_skips WHERE gid=?",((row["gid"],) for row in rows))
                    db.executemany("""INSERT INTO cloud_upload_skips VALUES(?,?,?,?,?) ON CONFLICT(gid) DO UPDATE SET
                        payload=excluded.payload,dataset_id=excluded.dataset_id,revision=excluded.revision,verified_at=excluded.verified_at""",
                        ((item["gid"],row_by_gid[item["gid"]]["payload"],result["dataset_id"],item["revision"],int(time.time())) for item in skipped))
                    if result.get("source_status") in {"review", "trusted"}:
                        self._put(db, "cloud_source_status", result["source_status"])
            with self.lock:
                if skipped:self.upload_min_interval_days=result["upload_min_interval_days"]
                self.uploaded += result["accepted"]
                self.pending += result["pending"]
                self.session_upload_processed += len(items)
                if self.upload_remaining is not None:
                    self.upload_remaining = max(0,self.upload_remaining-len(items))
                    self.upload_confirmed = self.upload_total - self.upload_remaining
            for row in rows:self.preflight_needed.pop(row["gid"],None)
            self._upload_progress()
            if self.stopped.wait(2.1):
                return False
        self._refresh_upload_snapshots()
        return self._count_upload_queue() > 0

    @staticmethod
    def _read_snapshot(db, gid):
        row = db.execute("""SELECT f.*,CASE WHEN r.checked_at=f.checked_at AND r.source=f.source THEN r.rating_count END AS rating_count
            FROM favorites f LEFT JOIN rating_counts r ON r.gid=f.gid WHERE f.gid=?""", (gid,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _write_snapshot(db, gid, value, clear_failures=False):
        if value is None:
            db.execute("DELETE FROM favorites WHERE gid=?", (gid,))
            db.execute("DELETE FROM rating_counts WHERE gid=?", (gid,))
            return
        db.execute("""INSERT INTO favorites VALUES(?,?,?,?,?,?) ON CONFLICT(gid) DO UPDATE SET
            favorite_count=excluded.favorite_count,checked_at=excluded.checked_at,source=excluded.source,
            collector_id=excluded.collector_id,collector_name=excluded.collector_name""",
            tuple(value[k] for k in ("gid", "favorite_count", "checked_at", "source", "collector_id", "collector_name")))
        if value["rating_count"] is None:
            db.execute("DELETE FROM rating_counts WHERE gid=?", (gid,))
        else:
            db.execute("""INSERT INTO rating_counts VALUES(?,?,?,?) ON CONFLICT(gid) DO UPDATE SET
                rating_count=excluded.rating_count,checked_at=excluded.checked_at,source=excluded.source""",
                tuple(value[k] for k in ("gid", "rating_count", "checked_at", "source")))
        if clear_failures:
            db.execute("DELETE FROM collection_failures WHERE gid=? AND failed_at<=?", (gid, value["checked_at"]))

    def _apply_event(self, db, event):
        gid, incoming = event["gid"], event["snapshot"]
        current = self._read_snapshot(db, gid)
        tracked = db.execute("SELECT payload,original FROM cloud_applied WHERE gid=?", (gid,)).fetchone()
        matches = bool(current and tracked and encode_snapshot(current) == tracked["payload"])
        baseline = json.loads(tracked["original"]) if matches and tracked["original"] else None
        if event["action"] == "correction":
            # 只撤销仍在使用的云端值；较新的自采/导入覆盖不会因回滚丢失。
            if not matches:
                if incoming is None or current and current["checked_at"] > incoming["checked_at"]:
                    return 0
                baseline = current if current != incoming else None
            own = db.execute("SELECT payload FROM cloud_self_snapshots WHERE gid=?", (gid,)).fetchone()
            own = json.loads(own[0]) if own else None
            candidates = [v for v in (baseline, own, incoming) if v is not None]
            selected = max(candidates, key=lambda v: v["checked_at"]) if candidates else None
            if selected is not None and selected != incoming:
                self._write_snapshot(db, gid, selected)
                db.execute("DELETE FROM cloud_applied WHERE gid=?", (gid,))
                return int(selected != current)
            if incoming is None:
                self._write_snapshot(db, gid, None)
                db.execute("DELETE FROM cloud_applied WHERE gid=?", (gid,))
                return int(current is not None)
        else:
            if current and current["checked_at"] > incoming["checked_at"]:
                return 0
            if not matches:
                baseline = current if current != incoming else None
        self._write_snapshot(db, gid, incoming, event["action"] == "snapshot")
        db.execute("""INSERT INTO cloud_applied VALUES(?,?,?) ON CONFLICT(gid) DO UPDATE SET
            payload=excluded.payload,original=excluded.original""",
            (gid, encode_snapshot(incoming), encode_snapshot(baseline) if baseline else None))
        return 1

    def _apply_page(self, page, previous, until, dataset):
        fields = {"format", "version", "dataset_id", "cursor", "until", "head", "has_more", "records"}
        if (not isinstance(page, dict) or set(page) != fields or page["format"] != FORMAT or type(page["version"]) is not int
                or page["version"] != VERSION or not re.fullmatch(r"[0-9a-f]{32}", str(page["dataset_id"]))
                or dataset and page["dataset_id"] != dataset
                or type(page["has_more"]) is not bool or not isinstance(page["records"], list) or len(page["records"]) > 500
                or any(type(page[key]) is not int or not previous <= page[key] <= 2**53-1 for key in ("cursor", "until", "head"))
                or not page["cursor"] <= page["until"] <= page["head"]
                or until is not None and page["until"] != until
                or page["has_more"] and (not page["records"] or page["cursor"] <= previous)
                or not page["has_more"] and page["cursor"] != page["until"]):
            raise ValueError("公共数据分页格式不正确，本地资料保留。")
        revision = previous
        for item in page["records"]:
            if (not isinstance(item, dict) or set(item) != {"gid", "revision", "action", "snapshot"}
                    or type(item["gid"]) is not int or not 1 <= item["gid"] <= 2**53-1
                    or item["action"] not in {"snapshot", "correction"}
                    or type(item["revision"]) is not int or not revision < item["revision"] <= page["cursor"]):
                raise ValueError("公共变更字段或修订号不正确，未写入本地资料。")
            value = item["snapshot"]
            if value is None:
                if item["action"] != "correction":
                    raise ValueError("普通快照不能为空。")
            else:
                if (not isinstance(value, dict) or set(value) != FIELDS or value["gid"] != item["gid"]
                        or type(value["gid"]) is not int
                        or type(value["favorite_count"]) is not int or not 0 <= value["favorite_count"] <= MAX_COUNT
                        or value["rating_count"] is not None and (type(value["rating_count"]) is not int or not 0 <= value["rating_count"] <= MAX_COUNT)
                        or type(value["checked_at"]) is not int or not 1 <= value["checked_at"] <= int(time.time())+300
                        or value["source"] not in {"e-hentai.org", "exhentai.org"}):
                    raise ValueError("公共快照字段、时间或数值不正确。")
                validate_collector(value["collector_id"], value["collector_name"], allow_unknown=True)
            revision = item["revision"]
        if page["has_more"] and revision != page["cursor"]:
            raise ValueError("公共分页游标不正确，未写入本地资料。")
        changed = 0
        with self.gate:
            self._available()
            with self.store_provider().connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for item in page["records"]:
                    changed += self._apply_event(db, item)
                    if item["action"]=="correction":self._invalidate_skip(db,item["gid"],item["revision"])
                self._put(db, "cloud_cursor", page["cursor"])
                self._put(db, "cloud_dataset", page["dataset_id"])
        with self.lock:
            self.downloaded += changed
            self.received += len(page["records"])
            self.session_download_received += len(page["records"])
        self._download_progress()
        return page["cursor"], page["until"], page["dataset_id"]

    def _download(self, config):
        with self.gate, self.store_provider().connection() as db:
            cursor = int(self._get(db, "cloud_cursor", "0"))
            dataset = self._get(db, "cloud_dataset")
        until, resets = None, 0
        for _ in range(50):
            self._available()
            self._download_progress()
            params = {"after": cursor, "limit": 500}
            if dataset:
                params["dataset_id"] = dataset
            if until is not None:
                params["until"] = until
            try:
                page = self.transport(config["endpoint"], "/v3/changes?" + urlencode(params))
            except SyncReset:
                if resets:
                    raise ValueError("公共库连续发生变化，请稍后重新同步。") from None
                cursor, dataset, until, resets = 0, "", None, resets+1
                with self.gate, self.store_provider().connection() as db:
                    for row in db.execute("SELECT gid,payload,original FROM cloud_applied").fetchall():
                        current = self._read_snapshot(db, row["gid"])
                        if current and encode_snapshot(current) == row["payload"]:
                            baseline = json.loads(row["original"]) if row["original"] else None
                            own = db.execute("SELECT payload FROM cloud_self_snapshots WHERE gid=?", (row["gid"],)).fetchone()
                            own = json.loads(own[0]) if own else None
                            candidates = [v for v in (baseline, own) if v is not None]
                            self._write_snapshot(db, row["gid"], max(candidates, key=lambda v:v["checked_at"]) if candidates else None)
                    db.execute("DELETE FROM cloud_applied")
                    db.execute("DELETE FROM cloud_upload_ack")
                    db.execute("DELETE FROM app_settings WHERE key='cloud_source_id'")
                    self._put(db, "cloud_cursor", 0)
                    self._put(db, "cloud_dataset", "")
                continue
            cursor, until, dataset = self._apply_page(page, cursor, until, dataset)
            if not page["has_more"]:
                return False
        return True

    def run_once(self):
        with self.gate, self.lock:
            config = self._config()
            if self.busy or not config["enabled"]:
                return
            self._available()
            self.busy = True
            self.phase = "checking"
            self.message = "正在检查同步队列…"
            self.downloaded = self.uploaded = self.pending = 0
            self.received = 0
        try:
            # 上传错误不阻止公开数据下载。
            upload_error = ""
            upload_delay = 300
            try:
                upload_more = self._upload(config)
            except ValueError as error:
                if isinstance(error, UploadBlocked):
                    with self.gate, self.store_provider().connection() as db:
                        self._put(db, "cloud_source_status", "blocked")
                upload_error, upload_more = str(error), False
                if isinstance(error, SyncRateLimited):
                    upload_delay = error.retry_after
                    self.upload_retry_until = time.monotonic()+upload_delay
            download_delay = None
            download_error = ""
            try:
                download_more = self._download(config) if config["download_enabled"] else False
            except SyncRateLimited as error:
                download_more = False
                download_delay = error.retry_after
                download_error = str(error)
            with self.lock:
                if (config["download_enabled"] and not download_error) or (config["upload_enabled"] and not upload_error):
                    self.last_success = int(time.time())
                summary = f"本轮本地新增或更新 {self.downloaded:,} 条，公开接受上传 {self.uploaded:,} 条，待审核 {self.pending:,} 条。"
                if upload_error or download_error:
                    self.phase = "retrying"
                    self.message = (("上传未完成：" + upload_error + " ") if upload_error else "")
                    self.message += (("公共下载限流：" + download_error + " ") if download_error else "") + summary
                    if download_more:
                        self.message += " 公共数据仍有后续分页。"
                    delay = max(upload_delay if upload_error else 0, download_delay or 0)
                    if upload_error:
                        self.message += f" {upload_delay} 秒后重试上传；公共下载继续独立检查。"
                    if download_error:
                        self.message += f" {download_delay} 秒后重试公共下载。"
                    if config["download_enabled"] and not download_delay:
                        delay = min(delay, 900)
                elif upload_more or download_more:
                    self.phase = "continuing"
                    self.message = "分批同步进行中：" + summary
                    if upload_more:
                        self.message += f" 仍有 {self.upload_remaining:,} 条待上传。"
                    if download_more:
                        self.message += " 公共数据仍有后续分页。"
                    self.message += " 5 秒后继续下一批。"
                    delay = 5
                else:
                    self.phase = "waiting"
                    self.message = "同步完成：" + summary + " 15 分钟后再次检查。"
                    delay = 900
                self.next_due = time.monotonic() + delay
        except Exception as error:
            with self.lock:
                self.message = str(error) if isinstance(error, ValueError) else "同步暂时未完成，本地资料保留；稍后自动重试。"
                self.phase = "retrying"
                self.next_due = time.monotonic() + 300
        finally:
            with self.lock:
                self.busy = False

    def _loop(self):
        while not self.stopped.is_set():
            try:
                if self._config()["enabled"] and time.monotonic() >= self.next_due and not self.maintenance_busy():
                    self.run_once()
            except Exception:
                with self.lock:
                    self.message = "资料库正在维护，同步稍后重试。"
                    self.phase = "retrying"
                    self.next_due = time.monotonic()+60
            self.wake.wait(5)
            self.wake.clear()

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
