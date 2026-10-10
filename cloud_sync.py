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
VERSION = 2
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
    headers = {"Accept": "application/json", "User-Agent": "ExCatalog-CloudSync/2"}
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
        error.close()
        if status == 409 and path.startswith("/v2/changes"):
            raise SyncReset("公共库已更换或恢复，正在重新下载。") from None
        if status == 401 and path == "/v2/contribute":
            raise UploadIdentityMissing("上传身份需重新登记，稍后重试。") from None
        if status == 403 and path in {"/v2/register", "/v2/contribute"}:
            raise UploadBlocked("该上传来源已被管理员封禁；公共下载继续可用。") from None
        messages = {401: "上传身份未登记或不匹配，请检查同步身份设置。", 403: "该上传来源已被管理员封禁；公共下载继续可用。", 426: "同步服务需要新版客户端，请更新后重试。", 429: "同步服务限流，稍后自动重试。",
                    400: "同步数据或采集身份不符合服务要求。",
                    503: "上传可能已暂停、候选容量已满或服务尚未就绪，请检查云端管理页。"}
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
        self.session_upload_processed = self.session_download_received = 0
        self.next_due = 0
        self._seed_self()
        with self.gate, self.store_provider().connection() as db:
            if self._get(db, "cloud_protocol") != "2":
                self._put(db, "cloud_cursor", 0)
                self._put(db, "cloud_dataset", "")
                db.execute("DELETE FROM cloud_upload_ack")
                self._put(db, "cloud_protocol", "2")
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
                    "enabled": download or upload, "download_enabled": download, "upload_enabled": upload,
                    "configured": bool(db.execute("SELECT 1 FROM app_settings WHERE key IN ('cloud_enabled','cloud_download_enabled')").fetchone())}

    def status(self):
        config = self._config()
        source_id, source_status = self._identity_status()
        with self.lock:
            return {**config, "busy": self.busy, "source_id": source_id, "source_status": source_status, "message": self.message,
                    "last_success": self.last_success, "downloaded": self.downloaded, "uploaded": self.uploaded,
                    "pending": self.pending, "interval_minutes": 15, "phase": self.phase,
                    "upload_remaining": self.upload_remaining, "session_upload_processed": self.session_upload_processed,
                    "session_download_received": self.session_download_received,
                    "next_sync_at": int(time.time()+max(0,self.next_due-time.monotonic()))
                        if config["enabled"] and not self.busy and self.next_due else None}

    def configure(self, value):
        # Accept the older local API field as a download preference during upgrades.
        if set(value) == {"endpoint", "enabled", "upload_enabled", "consent"}:
            value = {**value, "download_enabled": value["enabled"]}
            del value["enabled"]
        if set(value) != {"endpoint", "download_enabled", "upload_enabled", "consent"}:
            raise ValueError("在线同步设置字段不正确。")
        endpoint = validate_endpoint(value["endpoint"])
        if any(type(value[key]) is not bool for key in ("download_enabled", "upload_enabled", "consent")):
            raise ValueError("同步选项须为开关值。")
        if value["upload_enabled"] and not value["consent"]:
            raise ValueError("启用上传前，请确认公开分享自采快照及采集者信息。")
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
            self.message = "设置已保存；上传与下载独立运行，新来源的提交先待审核。" if enabled else "在线同步已关闭。"
            self.phase = "queued" if enabled else "idle"
            self.upload_remaining = None
            self.next_due = 0
            self.wake.set()
        return self.status()

    def trigger(self):
        with self.gate, self.lock:
            if not self._config()["enabled"]:
                raise ValueError("请先保存并启用在线同步。")
            if self.busy or self.maintenance_busy():
                raise ValueError("同步或维护正在进行，请等待完成。")
            self.next_due = 0
            self.phase = "queued"
            self.message = "已安排立即同步，将继续未完成的队列。"
            self.wake.set()
        return self.status()

    @staticmethod
    def _clear_identity(db):
        db.execute("DELETE FROM app_settings WHERE key IN ('cloud_upload_token','cloud_upload_owner','cloud_upload_endpoint','cloud_source_id','cloud_source_status')")

    def _identity_status(self):
        with self.gate, self.store_provider().connection() as db:
            return self._get(db, "cloud_source_id"), self._get(db, "cloud_source_status", "unregistered")

    def _upload_identity(self, config):
        with self.gate, self.store_provider().connection() as db:
            identity = read_identity(db)
            if (self._get(db, "cloud_upload_owner") != identity["collector_id"]
                    or self._get(db, "cloud_upload_endpoint") != config["endpoint"]):
                self._clear_identity(db)
                db.execute("DELETE FROM cloud_upload_ack")
            token = self._get(db, "cloud_upload_token")
            if token and self._get(db, "cloud_source_id"):
                return token
            if not token:
                token = "src_" + secrets.token_urlsafe(32)
                self._put(db, "cloud_upload_token", token)
                self._put(db, "cloud_upload_owner", identity["collector_id"])
                self._put(db, "cloud_upload_endpoint", config["endpoint"])
        result = self.transport(config["endpoint"], "/v2/register", identity, token)
        if (not isinstance(result, dict) or set(result) != {"version", "source_id", "status"}
                or result["version"] != VERSION or result["status"] not in {"review", "trusted"}):
            raise ValueError("上传身份登记结果不正确。")
        validate_collector(result["source_id"], "")
        with self.gate, self.store_provider().connection() as db:
            self._put(db, "cloud_source_id", result["source_id"])
            self._put(db, "cloud_source_status", result["status"])
        return token

    def _available(self):
        if self.stopped.is_set() or self.maintenance_busy():
            raise ValueError("正在停止服务或维护资料库，同步已暂停。")

    def _count_upload_queue(self):
        with self.gate:
            self._available()
            with self.store_provider().connection() as db:
                own = read_identity(db)["collector_id"]
                remaining = db.execute("""SELECT COUNT(*) FROM cloud_self_snapshots o
                    LEFT JOIN cloud_upload_ack a ON a.gid=o.gid
                    WHERE o.collector_id=? AND o.payload IS NOT a.payload""", (own,)).fetchone()[0]
        with self.lock:
            self.upload_remaining = remaining
        return remaining

    def _upload_progress(self):
        with self.lock:
            self.phase = "uploading"
            remaining = f"待上传 {self.upload_remaining:,} 条" if self.upload_remaining is not None else "正在检查待上传数量"
            self.message = f"正在上传自采快照：本次运行累计已提交 {self.session_upload_processed:,} 条，{remaining}。"

    def _download_progress(self):
        with self.lock:
            self.phase = "downloading"
            self.message = f"正在下载公共数据：本轮已接收 {self.received:,} 条，本地新增或更新 {self.downloaded:,} 条。"

    def _upload(self, config):
        if not config["upload_enabled"]:
            with self.lock:
                self.upload_remaining = None
            return False
        if not self._count_upload_queue():
            return False
        token = self._upload_identity(config)
        for _ in range(20):
            self._available()
            self._upload_progress()
            with self.gate, self.store_provider().connection() as db:
                own = read_identity(db)["collector_id"]
                rows = db.execute("""SELECT o.gid,o.payload FROM cloud_self_snapshots o
                    LEFT JOIN cloud_upload_ack a ON a.gid=o.gid
                    WHERE o.collector_id=? AND o.payload IS NOT a.payload ORDER BY o.gid LIMIT 5""", (own,)).fetchall()
            if not rows:
                with self.lock:
                    self.upload_remaining = 0
                return False
            items = [json.loads(row["payload"]) for row in rows]
            try:
                result = self.transport(config["endpoint"], "/v2/contribute", {"version": VERSION, "records": items}, token)
            except UploadIdentityMissing:
                with self.gate, self.store_provider().connection() as db:
                    db.execute("DELETE FROM app_settings WHERE key='cloud_source_id'")
                raise
            if (not isinstance(result, dict) or result.get("version") != VERSION
                    or any(type(result.get(key)) is not int or not 0 <= result[key] <= len(items) for key in ("accepted", "pending", "kept"))
                    or sum(result[key] for key in ("accepted", "pending", "kept")) != len(items)):
                raise ValueError("上传结果不正确，保留本地队列以便重试。")
            with self.gate:
                self._available()
                with self.store_provider().connection() as db:
                    db.executemany("INSERT INTO cloud_upload_ack VALUES(?,?) ON CONFLICT(gid) DO UPDATE SET payload=excluded.payload",
                                   ((row["gid"], row["payload"]) for row in rows))
                    if result.get("source_status") in {"review", "trusted"}:
                        self._put(db, "cloud_source_status", result["source_status"])
            with self.lock:
                self.uploaded += result["accepted"]
                self.pending += result["pending"]
                self.session_upload_processed += len(items)
                if self.upload_remaining is not None:
                    self.upload_remaining = max(0,self.upload_remaining-len(items))
            self._upload_progress()
            if self.stopped.wait(2.1):
                return False
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
                validate_collector(value["collector_id"], value["collector_name"])
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
                page = self.transport(config["endpoint"], "/v2/changes?" + urlencode(params))
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
            try:
                upload_more = self._upload(config)
            except ValueError as error:
                if isinstance(error, UploadBlocked):
                    with self.gate, self.store_provider().connection() as db:
                        self._put(db, "cloud_source_status", "blocked")
                upload_error, upload_more = str(error), False
            download_more = self._download(config) if config["download_enabled"] else False
            with self.lock:
                if config["download_enabled"] or not upload_error:
                    self.last_success = int(time.time())
                summary = f"本轮本地新增或更新 {self.downloaded:,} 条，公开接受上传 {self.uploaded:,} 条，待审核 {self.pending:,} 条。"
                if upload_error:
                    self.phase = "retrying"
                    self.message = "上传未完成：" + upload_error + " " + summary
                    if download_more:
                        self.message += " 公共数据仍有后续分页。"
                    self.message += " 5 分钟后重试。"
                    delay = 300
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
